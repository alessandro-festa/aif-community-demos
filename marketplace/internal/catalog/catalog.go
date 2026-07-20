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

// Action is the optional operation attached to a guide step.
type Action struct {
	// Type is one of: import | namespace-input | start-frontend | stop-frontend | open-url.
	Type string `yaml:"type" json:"type"`
	URL  string `yaml:"url" json:"url"`
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
