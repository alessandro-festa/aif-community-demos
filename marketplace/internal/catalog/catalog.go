// Package catalog discovers blueprints in a checkout directory by reading each
// blueprints/<id>/marketplace.yaml, and exposes them to the server.
package catalog

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"gopkg.in/yaml.v3"
)

// Prerequisite is a single cluster readiness check shown on a blueprint's page.
type Prerequisite struct {
	ID    string `yaml:"id" json:"id"`
	Label string `yaml:"label" json:"label"`
	// Kind is one of: crd | default-storageclass | namespace.
	Kind string `yaml:"kind" json:"kind"`
	Name string `yaml:"name" json:"name"`
}

// PortForward declares a kubectl port-forward the marketplace should open before
// launching a local frontend.
type PortForward struct {
	Name    string `yaml:"name" json:"name"`
	Service string `yaml:"service" json:"service"`
	Local   int    `yaml:"local" json:"local"`
	Remote  int    `yaml:"remote" json:"remote"`
}

// LocalFrontend describes a blueprint's UI that the marketplace runs locally.
type LocalFrontend struct {
	Dir          string            `yaml:"dir" json:"dir"`
	Runtime      string            `yaml:"runtime" json:"runtime"` // "python" (default)
	Install      []string          `yaml:"install" json:"install"` // pip invocations, in order
	Entry        string            `yaml:"entry" json:"entry"`     // uvicorn target, e.g. app.main:app
	Port         int               `yaml:"port" json:"port"`
	OpenPath     string            `yaml:"openPath" json:"openPath"`
	Env          map[string]string `yaml:"env" json:"env"`
	PortForwards []PortForward     `yaml:"portForwards" json:"portForwards"`
}

// ComponentUI is an in-cluster component web UI (e.g. Airflow) the marketplace
// can port-forward on demand — the button is enabled only once the service is
// Ready.
type ComponentUI struct {
	Name    string `yaml:"name" json:"name"`
	Label   string `yaml:"label" json:"label"`
	Service string `yaml:"service" json:"service"`
	Port    int    `yaml:"port" json:"port"`   // remote service port
	Local   int    `yaml:"local" json:"local"` // local port (default = Port)
	Path    string `yaml:"path" json:"path"`   // URL path (default "/")
}

// Action is the optional operation attached to a guide step.
type Action struct {
	// Type is one of: import | namespace-input | start-frontend | stop-frontend | open-url.
	Type string `yaml:"type" json:"type"`
	URL  string `yaml:"url" json:"url"`
}

// WizardOption is one toggleable choice in an import wizard. When selected, its
// Inject block is deep-merged into the target component's Helm values in the
// Blueprint CR before it is applied (maps merge recursively, lists append).
type WizardOption struct {
	ID          string         `yaml:"id" json:"id"`
	Label       string         `yaml:"label" json:"label"`
	Description string         `yaml:"description" json:"description"`
	Default     bool           `yaml:"default" json:"default"`
	Inject      map[string]any `yaml:"inject" json:"inject"`
}

// WizardInput is a free-text/secret value the import wizard collects before apply
// (e.g. a HuggingFace token needed to pull a gated model). The entered value is
// substituted for the "{{value}}" placeholder wherever it appears in Inject's
// string values, and the result is deep-merged into the target component — so the
// value flows into the Blueprint CR at import time (before AI Factory sees it).
type WizardInput struct {
	ID          string         `yaml:"id" json:"id"`
	Label       string         `yaml:"label" json:"label"`
	Description string         `yaml:"description" json:"description"`
	Placeholder string         `yaml:"placeholder" json:"placeholder"`
	Secret      bool           `yaml:"secret" json:"secret"`     // render as a password field
	Required    bool           `yaml:"required" json:"required"` // block import if empty
	Inject      map[string]any `yaml:"inject" json:"inject"`
	// Replace, when set, is a placeholder string replaced by the entered value
	// everywhere in the rendered CR — the way to fill a value inside a list item
	// (e.g. a per-model hf_token) where a deep-merge can't reach. json:"-" so the
	// placeholder token is never shipped to the browser.
	Replace string `yaml:"replace" json:"-"`
}

// ImportWizard, when present on a blueprint, makes the import guide step show a
// checklist (Options) and/or a set of text inputs (Inputs). Each selected option's
// Inject and each filled input's (substituted) Inject are merged into the values of
// the spec.components entry whose chartName == TargetComponent.
type ImportWizard struct {
	Title           string         `yaml:"title" json:"title"`
	Body            string         `yaml:"body" json:"body"`
	TargetComponent string         `yaml:"targetComponent" json:"targetComponent"`
	Options         []WizardOption `yaml:"options" json:"options"`
	Inputs          []WizardInput  `yaml:"inputs" json:"inputs,omitempty"`
}

// Option returns the wizard option with the given id.
func (w *ImportWizard) Option(id string) (WizardOption, bool) {
	for _, o := range w.Options {
		if o.ID == id {
			return o, true
		}
	}
	return WizardOption{}, false
}

// Input returns the wizard input with the given id.
func (w *ImportWizard) Input(id string) (WizardInput, bool) {
	for _, in := range w.Inputs {
		if in.ID == id {
			return in, true
		}
	}
	return WizardInput{}, false
}

// ModelSizes, when present on a (CPU) blueprint, lets the user pick an LLM size in
// the guided demo. The chosen option's Model is substituted for the Replace token in
// the CR at import time (so ollama pulls/serves it) and, when EnvKey is set, injected
// into the local frontend's env at start time (so the app requests it).
type ModelSizes struct {
	Default string        `yaml:"default" json:"default"`
	EnvKey  string        `yaml:"envKey" json:"envKey,omitempty"` // frontend env var to set (optional)
	Replace string        `yaml:"replace" json:"-"`               // CR placeholder token (never shipped to the browser)
	Options []ModelOption `yaml:"options" json:"options"`
}

// ModelOption is one selectable model size.
type ModelOption struct {
	ID    string `yaml:"id" json:"id"`
	Label string `yaml:"label" json:"label"`
	Model string `yaml:"model" json:"model"`
}

// Option returns the model option with the given id.
func (m *ModelSizes) Option(id string) (ModelOption, bool) {
	for _, o := range m.Options {
		if o.ID == id {
			return o, true
		}
	}
	return ModelOption{}, false
}

// Resolve returns the model string for the given size id, falling back to Default
// (then the first option). Returns "" if there are no options.
func (m *ModelSizes) Resolve(id string) string {
	if o, ok := m.Option(id); ok {
		return o.Model
	}
	if o, ok := m.Option(m.Default); ok {
		return o.Model
	}
	if len(m.Options) > 0 {
		return m.Options[0].Model
	}
	return ""
}

// GuideStep is one page of the step-by-step demo.
type GuideStep struct {
	Title  string  `yaml:"title" json:"title"`
	Body   string  `yaml:"body" json:"body"`
	Action *Action `yaml:"action" json:"action,omitempty"`
}

// Blueprint is the full metadata for one catalog entry.
type Blueprint struct {
	ID            string         `yaml:"id" json:"id"`
	DisplayName   string         `yaml:"displayName" json:"displayName"`
	Description   string         `yaml:"description" json:"description"`
	Category      string         `yaml:"category" json:"category"`
	Tags          []string       `yaml:"tags" json:"tags"`
	BlueprintFile string         `yaml:"blueprintFile" json:"blueprintFile"`
	Prerequisites []Prerequisite `yaml:"prerequisites" json:"prerequisites"`
	LocalFrontend *LocalFrontend `yaml:"localFrontend" json:"localFrontend,omitempty"`
	ComponentUIs  []ComponentUI  `yaml:"componentUIs" json:"componentUIs,omitempty"`
	ImportWizard  *ImportWizard  `yaml:"importWizard" json:"importWizard,omitempty"`
	ModelSizes    *ModelSizes    `yaml:"modelSizes" json:"modelSizes,omitempty"`
	Guide         []GuideStep    `yaml:"guide" json:"guide"`

	// Dir is the absolute path to this blueprint's folder in the checkout
	// (populated at load time, not from YAML).
	Dir string `yaml:"-" json:"-"`
}

// Catalog is the loaded set of blueprints, safe for concurrent reads/reloads.
type Catalog struct {
	mu   sync.RWMutex
	root string
	bps  []Blueprint
}

// New returns an empty catalog rooted at the given blueprints directory.
func New(root string) *Catalog { return &Catalog{root: root} }

// Root returns the directory the catalog scans.
func (c *Catalog) Root() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.root
}

// SetRoot points the catalog at a new blueprints directory and reloads.
func (c *Catalog) SetRoot(root string) error {
	c.mu.Lock()
	c.root = root
	c.mu.Unlock()
	return c.Reload()
}

// Reload rescans the root for blueprints/<id>/marketplace.yaml files.
func (c *Catalog) Reload() error {
	c.mu.RLock()
	root := c.root
	c.mu.RUnlock()

	entries, err := os.ReadDir(root)
	if err != nil {
		return fmt.Errorf("read blueprints dir %s: %w", root, err)
	}
	var bps []Blueprint
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		metaPath := filepath.Join(root, e.Name(), "marketplace.yaml")
		data, err := os.ReadFile(metaPath)
		if err != nil {
			continue // not a blueprint folder
		}
		var bp Blueprint
		if err := yaml.Unmarshal(data, &bp); err != nil {
			return fmt.Errorf("parse %s: %w", metaPath, err)
		}
		if bp.ID == "" {
			bp.ID = e.Name()
		}
		bp.Dir = filepath.Join(root, e.Name())
		bps = append(bps, bp)
	}
	sort.Slice(bps, func(i, j int) bool { return bps[i].DisplayName < bps[j].DisplayName })

	c.mu.Lock()
	c.bps = bps
	c.mu.Unlock()
	return nil
}

// List returns all loaded blueprints.
func (c *Catalog) List() []Blueprint {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make([]Blueprint, len(c.bps))
	copy(out, c.bps)
	return out
}

// Get returns a blueprint by id.
func (c *Catalog) Get(id string) (Blueprint, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	for _, bp := range c.bps {
		if bp.ID == id {
			return bp, true
		}
	}
	return Blueprint{}, false
}
