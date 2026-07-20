// Package server exposes the marketplace REST + SSE API and serves the embedded
// web UI.
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"os/exec"
	"strings"
	"time"

	"github.com/suse/blueprint-marketplace/internal/catalog"
	"github.com/suse/blueprint-marketplace/internal/config"
	"github.com/suse/blueprint-marketplace/internal/kube"
	"github.com/suse/blueprint-marketplace/internal/proc"
)

// Server wires the API handlers to the catalog, config, and process manager.
type Server struct {
	cfg    *config.Store
	cat    *catalog.Catalog
	pm     *proc.Manager
	web    fs.FS
	resync func() error // re-pull git + reload catalog after a settings change
}

// New builds a Server. resync may be nil (e.g. when running with --dir).
func New(cfg *config.Store, cat *catalog.Catalog, pm *proc.Manager, web fs.FS, resync func() error) *Server {
	return &Server{cfg: cfg, cat: cat, pm: pm, web: web, resync: resync}
}

// Handler returns the root http.Handler (API + static SPA).
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/contexts", s.handleContexts)
	mux.HandleFunc("GET /api/settings", s.handleGetSettings)
	mux.HandleFunc("PUT /api/settings", s.handlePutSettings)
	mux.HandleFunc("GET /api/catalog", s.handleCatalog)
	mux.HandleFunc("GET /api/blueprints/{id}/prereqs", s.handlePrereqs)
	mux.HandleFunc("POST /api/blueprints/{id}/import", s.handleImport)
	mux.HandleFunc("POST /api/blueprints/{id}/frontend/start", s.handleFrontendStart)
	mux.HandleFunc("POST /api/blueprints/{id}/frontend/stop", s.handleFrontendStop)
	mux.HandleFunc("GET /api/processes", s.handleProcesses)

	// Static web UI (index.html fallback for the single page).
	fileServer := http.FileServer(http.FS(s.web))
	mux.Handle("/", fileServer)
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
	writeJSON(w, map[string]any{
		"blueprintsRepo": cfg.BlueprintsRepo,
		"blueprintsRef":  cfg.BlueprintsRef,
		"targetContext":  cfg.TargetContext,
		"gitManaged":     s.resync != nil,
	})
}

func (s *Server) handlePutSettings(w http.ResponseWriter, r *http.Request) {
	var in config.Config
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	prev := s.cfg.Get()
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
	results := kube.CheckPrereqs(ctx, s.targetContext(ctx), bp.Prerequisites)
	writeJSON(w, map[string]any{"context": s.targetContext(ctx), "results": results})
}

func (s *Server) handleImport(w http.ResponseWriter, r *http.Request) {
	bp, ok := s.cat.Get(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, fmt.Errorf("blueprint not found"))
		return
	}
	sse, flush, ok := beginSSE(w)
	if !ok {
		return
	}
	kubeCtx := s.targetContext(r.Context())
	if kubeCtx == "" {
		sse("error", "no target cluster selected (set one in Settings)")
		return
	}
	crPath := bp.Dir + "/" + bp.BlueprintFile
	sse("log", fmt.Sprintf("kubectl --context %s apply -f %s", kubeCtx, crPath))
	emit := func(line string) { sse("log", line); flush() }
	// Import can take a moment; use a generous timeout.
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Minute)
	defer cancel()
	if err := kube.Apply(ctx, kubeCtx, crPath, emit); err != nil {
		sse("error", err.Error())
		return
	}
	sse("done", "Blueprint imported")
}

type frontendReq struct {
	Namespace string `json:"namespace"`
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
	kubeCtx := s.targetContext(r.Context())
	emit := func(line string) { sse("log", line); flush() }
	sess, err := s.pm.StartFrontend(bp, kubeCtx, req.Namespace, emit)
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
	writeJSON(w, s.pm.List())
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
