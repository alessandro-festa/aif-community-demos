// Package server exposes the marketplace REST + SSE API and serves the embedded
// web UI.
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/suse/blueprint-marketplace/internal/catalog"
	"github.com/suse/blueprint-marketplace/internal/config"
	"github.com/suse/blueprint-marketplace/internal/kube"
	"github.com/suse/blueprint-marketplace/internal/proc"
	"github.com/suse/blueprint-marketplace/internal/rancher"
)

var kcSafeName = regexp.MustCompile(`[^a-zA-Z0-9._-]+`)

// Server wires the API handlers to the catalog, config, and process manager.
type Server struct {
	cfg      *config.Store
	cat      *catalog.Catalog
	pm       *proc.Manager
	web      fs.FS
	resync   func() error // re-pull git + reload catalog after a settings change
	kubeBase string       // KUBECONFIG present at launch (empty = kubectl default)

	// Rancher connection — the API token is kept in memory only (never persisted).
	rancherMu    sync.Mutex
	rancherURL   string
	rancherToken string
	rancherInsec bool
}

// New builds a Server. resync may be nil (e.g. when running with --dir).
func New(cfg *config.Store, cat *catalog.Catalog, pm *proc.Manager, web fs.FS, resync func() error) *Server {
	s := &Server{cfg: cfg, cat: cat, pm: pm, web: web, resync: resync,
		kubeBase: os.Getenv("KUBECONFIG")}
	s.applyKubeconfig() // merge any previously-imported kubeconfigs into KUBECONFIG
	return s
}

// applyKubeconfig sets the process KUBECONFIG to the launch/default kubeconfig
// merged with the imported kubeconfig files, so every `kubectl` call (which
// inherits the process env) sees all contexts. Called at startup and whenever the
// imported set changes.
func (s *Server) applyKubeconfig() {
	extras := s.cfg.Get().Kubeconfigs
	if len(extras) == 0 {
		if s.kubeBase != "" {
			_ = os.Setenv("KUBECONFIG", s.kubeBase)
		}
		return
	}
	base := s.kubeBase
	if base == "" {
		if home, err := os.UserHomeDir(); err == nil {
			base = filepath.Join(home, ".kube", "config")
		}
	}
	parts := filepath.SplitList(base)
	parts = append(parts, extras...)
	_ = os.Setenv("KUBECONFIG", strings.Join(parts, string(os.PathListSeparator)))
}

// Handler returns the root http.Handler (API + static SPA).
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/contexts", s.handleContexts)
	mux.HandleFunc("GET /api/settings", s.handleGetSettings)
	mux.HandleFunc("PUT /api/settings", s.handlePutSettings)
	mux.HandleFunc("POST /api/kubeconfig/import", s.handleKubeconfigImport)
	mux.HandleFunc("POST /api/kubeconfig/remove", s.handleKubeconfigRemove)
	mux.HandleFunc("POST /api/rancher/connect", s.handleRancherConnect)
	mux.HandleFunc("POST /api/rancher/clusters/import", s.handleRancherImport)
	mux.HandleFunc("GET /api/catalog", s.handleCatalog)
	mux.HandleFunc("GET /api/blueprints/{id}/prereqs", s.handlePrereqs)
	mux.HandleFunc("POST /api/blueprints/{id}/import", s.handleImport)
	mux.HandleFunc("POST /api/blueprints/{id}/frontend/start", s.handleFrontendStart)
	mux.HandleFunc("POST /api/blueprints/{id}/frontend/stop", s.handleFrontendStop)
	mux.HandleFunc("POST /api/blueprints/{id}/service-status", s.handleServiceStatus)
	mux.HandleFunc("POST /api/blueprints/{id}/component-ui/start", s.handleComponentUIStart)
	mux.HandleFunc("POST /api/blueprints/{id}/component-ui/stop", s.handleComponentUIStop)
	mux.HandleFunc("GET /api/processes", s.handleProcesses)

	// Static web UI. Send no-store so browsers always fetch the current embedded
	// assets (avoids stale cached app.js/css after a binary upgrade).
	fileServer := http.FileServer(http.FS(s.web))
	mux.Handle("/", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store, must-revalidate")
		fileServer.ServeHTTP(w, r)
	}))
	return mux
}

// targetContext resolves the kube context to act on: the configured one, else
// the host's current-context.
func (s *Server) targetContext(ctx context.Context) string {
	if c := s.cfg.Get().TargetContext; c != "" {
		return c
	}
	cmd := exec.CommandContext(ctx, "kubectl", "config", "current-context")
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// contextOr returns the caller-supplied kube context when non-empty, otherwise
// the settings-level target. This lets a single blueprint's guided demo (or a
// bulk import) act on a specific cluster — e.g. a Rancher downstream cluster —
// instead of the global default.
func (s *Server) contextOr(ctx context.Context, override string) string {
	if o := strings.TrimSpace(override); o != "" {
		return o
	}
	return s.targetContext(ctx)
}

func (s *Server) handleContexts(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	ctxs, err := kube.Contexts(ctx)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, ctxs)
}

func (s *Server) handleGetSettings(w http.ResponseWriter, r *http.Request) {
	cfg := s.cfg.Get()
	s.rancherMu.Lock()
	connected := s.rancherToken != ""
	s.rancherMu.Unlock()
	writeJSON(w, map[string]any{
		"blueprintsRepo":   cfg.BlueprintsRepo,
		"blueprintsRef":    cfg.BlueprintsRef,
		"targetContext":    cfg.TargetContext,
		"kubeconfigs":      cfg.Kubeconfigs,
		"gitManaged":       s.resync != nil,
		"rancherUrl":       cfg.RancherURL,
		"rancherInsecure":  cfg.RancherInsecure,
		"rancherConnected": connected, // token present in memory this session
	})
}

func (s *Server) handlePutSettings(w http.ResponseWriter, r *http.Request) {
	var in config.Config
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	prev := s.cfg.Get()
	in.Kubeconfigs = prev.Kubeconfigs         // managed via the kubeconfig import/remove endpoints
	in.RancherURL = prev.RancherURL           // managed via the rancher connect endpoint
	in.RancherInsecure = prev.RancherInsecure // managed via the rancher connect endpoint
	if err := s.cfg.Set(in); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	// If the repo/ref changed and we manage git, re-pull + reload the catalog.
	if s.resync != nil && (in.BlueprintsRepo != prev.BlueprintsRepo || in.BlueprintsRef != prev.BlueprintsRef) {
		if err := s.resync(); err != nil {
			writeErr(w, http.StatusBadGateway, fmt.Errorf("reload blueprints: %w", err))
			return
		}
	}
	s.handleGetSettings(w, r)
}

func kubeconfigDir() (string, error) {
	d, err := config.Dir()
	if err != nil {
		return "", err
	}
	kd := filepath.Join(d, "kubeconfigs")
	return kd, os.MkdirAll(kd, 0o700)
}

type kubeconfigReq struct {
	Name    string `json:"name"`    // label for a pasted kubeconfig
	Content string `json:"content"` // pasted kubeconfig YAML
	Path    string `json:"path"`    // OR an existing kubeconfig file path
}

// handleKubeconfigImport adds a kubeconfig — either pasted YAML (saved under the
// managed kubeconfigs dir) or an existing file path — to the merge set, so its
// contexts become selectable. Returns the updated kubeconfig list + contexts.
func (s *Server) handleKubeconfigImport(w http.ResponseWriter, r *http.Request) {
	var req kubeconfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	var path string
	if strings.TrimSpace(req.Content) != "" {
		if !validKubeconfig(req.Content) {
			writeErr(w, http.StatusBadRequest, fmt.Errorf("that doesn't look like a kubeconfig (no 'contexts')"))
			return
		}
		var err error
		path, err = s.writeManagedKubeconfig(req.Name, req.Content)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, err)
			return
		}
	} else if p := strings.TrimSpace(req.Path); p != "" {
		if strings.HasPrefix(p, "~/") {
			if home, err := os.UserHomeDir(); err == nil {
				p = filepath.Join(home, p[2:])
			}
		}
		if info, err := os.Stat(p); err != nil || info.IsDir() {
			writeErr(w, http.StatusBadRequest, fmt.Errorf("kubeconfig file not found: %s", p))
			return
		}
		path = p
	} else {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("provide kubeconfig content or a file path"))
		return
	}

	s.mergeAndRespond(w, r, path)
}

// writeManagedKubeconfig writes kubeconfig YAML into the managed kubeconfigs dir at
// 0600 under a sanitized <name>.yaml, returning the path. Callers validate first.
func (s *Server) writeManagedKubeconfig(name, content string) (string, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		name = "imported"
	}
	name = strings.TrimSuffix(kcSafeName.ReplaceAllString(name, "-"), ".yaml") + ".yaml"
	kd, err := kubeconfigDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(kd, name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		return "", err
	}
	return path, nil
}

// validKubeconfig reports whether content parses as YAML and has a 'contexts' key —
// a cheap guard so a bad merged file can't break `kubectl config` for every context.
func validKubeconfig(content string) bool {
	var kc map[string]any
	return yaml.Unmarshal([]byte(content), &kc) == nil && kc["contexts"] != nil
}

// mergeAndRespond adds path to the merge set, re-applies KUBECONFIG, and writes the
// standard {kubeconfigs, contexts} JSON response used by both kubeconfig and Rancher imports.
func (s *Server) mergeAndRespond(w http.ResponseWriter, r *http.Request, path string) {
	cfg := s.cfg.Get()
	if !slices.Contains(cfg.Kubeconfigs, path) {
		cfg.Kubeconfigs = append(cfg.Kubeconfigs, path)
		if err := s.cfg.Set(cfg); err != nil {
			writeErr(w, http.StatusInternalServerError, err)
			return
		}
	}
	s.applyKubeconfig()

	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	ctxs, err := kube.Contexts(ctx)
	if err != nil {
		writeErr(w, http.StatusBadGateway, fmt.Errorf("imported, but listing contexts failed: %w", err))
		return
	}
	writeJSON(w, map[string]any{"kubeconfigs": s.cfg.Get().Kubeconfigs, "contexts": ctxs})
}

// handleKubeconfigRemove drops a kubeconfig from the merge set (and deletes the
// file if it's one we manage).
func (s *Server) handleKubeconfigRemove(w http.ResponseWriter, r *http.Request) {
	var req kubeconfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	kd, _ := kubeconfigDir()
	cfg := s.cfg.Get()
	kept := make([]string, 0, len(cfg.Kubeconfigs))
	for _, p := range cfg.Kubeconfigs {
		if p == req.Path {
			if kd != "" && strings.HasPrefix(p, kd) {
				_ = os.Remove(p)
			}
			continue
		}
		kept = append(kept, p)
	}
	cfg.Kubeconfigs = kept
	if err := s.cfg.Set(cfg); err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	s.applyKubeconfig()

	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	ctxs, _ := kube.Contexts(ctx)
	writeJSON(w, map[string]any{"kubeconfigs": cfg.Kubeconfigs, "contexts": ctxs})
}

type rancherConnectReq struct {
	URL      string `json:"url"`
	Token    string `json:"token"`
	Insecure bool   `json:"insecure"`
}

// rancherClusterInfo is a downstream cluster plus whether we've already imported it.
type rancherClusterInfo struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Imported bool   `json:"imported"`
	Path     string `json:"path"` // where the imported kubeconfig lives (for removal)
}

// rancherKubeconfigPath returns the managed path a downstream cluster's kubeconfig
// is (or would be) written to — used to tag clusters as already-imported.
func rancherKubeconfigPath(name string) string {
	kd, err := kubeconfigDir()
	if err != nil {
		return ""
	}
	safe := strings.TrimSuffix(kcSafeName.ReplaceAllString("rancher-"+name, "-"), ".yaml") + ".yaml"
	return filepath.Join(kd, safe)
}

// handleRancherConnect stores the API token in memory (never persisted), remembers
// the URL + insecure flag, and lists the downstream clusters.
func (s *Server) handleRancherConnect(w http.ResponseWriter, r *http.Request) {
	var req rancherConnectReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if strings.TrimSpace(req.URL) == "" || strings.TrimSpace(req.Token) == "" {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("Rancher URL and API token are required"))
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 25*time.Second)
	defer cancel()
	client := rancher.New(req.URL, req.Token, req.Insecure)
	clusters, err := client.ListClusters(ctx)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}

	// Remember the connection (token in memory only) and persist the non-secret bits.
	s.rancherMu.Lock()
	s.rancherURL = strings.TrimRight(strings.TrimSpace(req.URL), "/")
	s.rancherToken = strings.TrimSpace(req.Token)
	s.rancherInsec = req.Insecure
	s.rancherMu.Unlock()
	cfg := s.cfg.Get()
	cfg.RancherURL = s.rancherURL
	cfg.RancherInsecure = req.Insecure
	_ = s.cfg.Set(cfg) // best-effort; a persist failure shouldn't block connecting

	imported := s.cfg.Get().Kubeconfigs
	out := make([]rancherClusterInfo, 0, len(clusters))
	for _, c := range clusters {
		p := rancherKubeconfigPath(c.Name)
		out = append(out, rancherClusterInfo{
			ID: c.ID, Name: c.Name, Path: p, Imported: p != "" && slices.Contains(imported, p),
		})
	}
	writeJSON(w, map[string]any{"url": s.rancherURL, "clusters": out})
}

type rancherImportReq struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// handleRancherImport generates a downstream cluster's kubeconfig via Rancher and
// merges it (reusing the kubeconfig machinery) so it becomes a selectable context.
func (s *Server) handleRancherImport(w http.ResponseWriter, r *http.Request) {
	var req rancherImportReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	if strings.TrimSpace(req.ID) == "" {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("cluster id is required"))
		return
	}
	s.rancherMu.Lock()
	url, token, insec := s.rancherURL, s.rancherToken, s.rancherInsec
	s.rancherMu.Unlock()
	if token == "" {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("connect to Rancher first"))
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	kubeconfig, err := rancher.New(url, token, insec).GenerateKubeconfig(ctx, req.ID)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	if !validKubeconfig(kubeconfig) {
		writeErr(w, http.StatusBadGateway, fmt.Errorf("Rancher returned an unexpected kubeconfig for %q", req.Name))
		return
	}
	name := req.Name
	if strings.TrimSpace(name) == "" {
		name = req.ID
	}
	path, err := s.writeManagedKubeconfig("rancher-"+name, kubeconfig)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err)
		return
	}
	s.mergeAndRespond(w, r, path)
}

func (s *Server) handleCatalog(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.cat.List())
}

func (s *Server) handlePrereqs(w http.ResponseWriter, r *http.Request) {
	bp, ok := s.cat.Get(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, fmt.Errorf("blueprint not found"))
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	kubeCtx := s.contextOr(ctx, r.URL.Query().Get("context"))
	results := kube.CheckPrereqs(ctx, kubeCtx, bp.Prerequisites)
	writeJSON(w, map[string]any{"context": kubeCtx, "results": results})
}

type importReq struct {
	// Selections are import-wizard option ids to inject before applying.
	Selections []string `json:"selections"`
	// Inputs are import-wizard input id -> entered value (e.g. an HF token),
	// substituted into that input's Inject template before applying.
	Inputs map[string]string `json:"inputs"`
	// Context, when set, is the kube context to apply into, overriding the
	// settings-level target. Used by bulk import so a batch can be installed
	// into a specific (e.g. Rancher downstream) cluster. Empty = settings target.
	Context string `json:"context"`
	// ModelSize, when the blueprint defines modelSizes, is the chosen size id; the
	// matching model is substituted for the CR placeholder before apply.
	ModelSize string `json:"modelSize"`
}

func (s *Server) handleImport(w http.ResponseWriter, r *http.Request) {
	bp, ok := s.cat.Get(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, fmt.Errorf("blueprint not found"))
		return
	}
	var req importReq
	_ = json.NewDecoder(r.Body).Decode(&req) // body optional

	sse, flush, ok := beginSSE(w)
	if !ok {
		return
	}
	// A request may name its target cluster (bulk import, or a guided demo pointed
	// at a downstream cluster); otherwise fall back to the settings-level target.
	kubeCtx := s.contextOr(r.Context(), req.Context)
	if kubeCtx == "" {
		sse("error", "no target cluster selected (set one in Settings)")
		return
	}
	emit := func(line string) { sse("log", line); flush() }

	// Enforce required wizard inputs (e.g. a HuggingFace token for a gated model)
	// before we touch the cluster.
	if bp.ImportWizard != nil {
		for _, in := range bp.ImportWizard.Inputs {
			if in.Required && strings.TrimSpace(req.Inputs[in.ID]) == "" {
				sse("error", fmt.Sprintf("%q is required", in.Label))
				return
			}
		}
	}

	// Build the manifest to apply. With an import wizard + selections/inputs, the
	// chosen options and entered values are merged into the target component;
	// otherwise the CR file is applied verbatim (preserving its comments). A
	// model-size choice then substitutes the CR placeholder with the chosen model.
	crPath := bp.Dir + "/" + bp.BlueprintFile
	applyPath := crPath
	var manifest []byte // nil = apply the CR file verbatim
	if bp.ImportWizard != nil && (len(req.Selections) > 0 || len(req.Inputs) > 0) {
		m, err := buildImportManifest(bp, req.Selections, req.Inputs)
		if err != nil {
			sse("error", err.Error())
			return
		}
		manifest = m
		if len(req.Selections) > 0 {
			sse("log", fmt.Sprintf("options selected: %s", strings.Join(req.Selections, ", ")))
		}
		// Never log input values — they may be secrets. Log which inputs were set.
		if len(req.Inputs) > 0 {
			var ids []string
			for id, v := range req.Inputs {
				if strings.TrimSpace(v) != "" {
					ids = append(ids, id)
				}
			}
			if len(ids) > 0 {
				sse("log", fmt.Sprintf("inputs provided: %s", strings.Join(ids, ", ")))
			}
		}
	}

	// Apply the model-size choice by substituting the CR placeholder with the model.
	if bp.ModelSizes != nil && bp.ModelSizes.Replace != "" {
		if model := bp.ModelSizes.Resolve(req.ModelSize); model != "" {
			if manifest == nil {
				raw, err := os.ReadFile(crPath)
				if err != nil {
					sse("error", err.Error())
					return
				}
				manifest = raw
			}
			manifest = bytes.ReplaceAll(manifest, []byte(bp.ModelSizes.Replace), []byte(model))
			sse("log", "model: "+model)
		}
	}

	// If we produced a modified manifest, apply it from a temp file.
	if manifest != nil {
		tmp, err := os.CreateTemp("", "bpm-import-*.yaml")
		if err != nil {
			sse("error", err.Error())
			return
		}
		defer os.Remove(tmp.Name())
		if _, err := tmp.Write(manifest); err != nil {
			tmp.Close()
			sse("error", err.Error())
			return
		}
		tmp.Close()
		applyPath = tmp.Name()
	}

	sse("log", fmt.Sprintf("kubectl --context %s apply -f %s", kubeCtx, applyPath))
	// Import can take a moment; use a generous timeout.
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Minute)
	defer cancel()
	if err := kube.Apply(ctx, kubeCtx, applyPath, emit); err != nil {
		sse("error", err.Error())
		return
	}
	sse("done", "Blueprint imported")
}

type frontendReq struct {
	Namespace string `json:"namespace"`
	Context   string `json:"context"`
	// ModelSize, when the blueprint defines modelSizes with an envKey, sets that env
	// var to the chosen model so the app requests it.
	ModelSize string `json:"modelSize"`
}

func (s *Server) handleFrontendStart(w http.ResponseWriter, r *http.Request) {
	bp, ok := s.cat.Get(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, fmt.Errorf("blueprint not found"))
		return
	}
	var req frontendReq
	_ = json.NewDecoder(r.Body).Decode(&req)

	sse, flush, ok := beginSSE(w)
	if !ok {
		return
	}
	kubeCtx := s.contextOr(r.Context(), req.Context)
	// Model-size choice → override the blueprint's model env var for this run.
	var envOverride map[string]string
	if bp.ModelSizes != nil && bp.ModelSizes.EnvKey != "" {
		if model := bp.ModelSizes.Resolve(req.ModelSize); model != "" {
			envOverride = map[string]string{bp.ModelSizes.EnvKey: model}
		}
	}
	emit := func(line string) { sse("log", line); flush() }
	sess, err := s.pm.StartFrontend(bp, kubeCtx, req.Namespace, envOverride, emit)
	if err != nil {
		sse("error", err.Error())
		return
	}
	sse("done", sess.URL)
}

func (s *Server) handleFrontendStop(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.pm.Stop(id)
	writeJSON(w, map[string]any{"stopped": id})
}

func (s *Server) handleProcesses(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.pm.Processes())
}

type svcStatusReq struct {
	Namespace string `json:"namespace"`
	Service   string `json:"service"`
	Context   string `json:"context"`
}

func (s *Server) handleServiceStatus(w http.ResponseWriter, r *http.Request) {
	var req svcStatusReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	ready, n := kube.ServiceReady(ctx, s.contextOr(ctx, req.Context), req.Namespace, req.Service)
	writeJSON(w, map[string]any{"ready": ready, "endpoints": n})
}

type componentUIReq struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Context   string `json:"context"`
}

func (s *Server) handleComponentUIStart(w http.ResponseWriter, r *http.Request) {
	bp, ok := s.cat.Get(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, fmt.Errorf("blueprint not found"))
		return
	}
	var req componentUIReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	var cui *catalog.ComponentUI
	for i := range bp.ComponentUIs {
		if bp.ComponentUIs[i].Name == req.Name {
			cui = &bp.ComponentUIs[i]
			break
		}
	}
	if cui == nil {
		writeErr(w, http.StatusNotFound, fmt.Errorf("component UI %q not found", req.Name))
		return
	}
	local := cui.Local
	if local == 0 {
		local = cui.Port
	}
	pf := catalog.PortForward{Name: cui.Name, Service: cui.Service, Local: local, Remote: cui.Port}
	p, err := s.pm.StartPortFwd(bp.ID, s.contextOr(r.Context(), req.Context), req.Namespace, cui.Path, pf)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"url": p.URL, "name": cui.Name})
}

func (s *Server) handleComponentUIStop(w http.ResponseWriter, r *http.Request) {
	var req componentUIReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	s.pm.StopPortFwd(r.PathValue("id"), req.Name)
	writeJSON(w, map[string]any{"stopped": req.Name})
}

// --- helpers ------------------------------------------------------------- //

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
}

// beginSSE prepares an SSE response and returns an emit + flush pair.
func beginSSE(w http.ResponseWriter) (func(event, data string), func(), bool) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, fmt.Errorf("streaming unsupported"))
		return nil, nil, false
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	emit := func(event, data string) {
		// SSE data must not contain raw newlines; send each line as its own data:.
		fmt.Fprintf(w, "event: %s\n", event)
		for _, line := range strings.Split(data, "\n") {
			fmt.Fprintf(w, "data: %s\n", line)
		}
		fmt.Fprint(w, "\n")
		flusher.Flush()
	}
	return emit, func() { flusher.Flush() }, true
}
