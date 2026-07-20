// Package proc runs and tracks the local processes a blueprint demo needs:
// kubectl port-forwards and the blueprint's own frontend (a Python venv +
// uvicorn). Each blueprint has at most one "session" (a group of processes)
// with a shared log hub so the UI can stream setup + runtime output.
package proc

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/suse/blueprint-marketplace/internal/catalog"
)

// groupKill makes a command run in its own process group and, when its context
// is cancelled, SIGKILL the whole group. This is essential because `kubectl` is
// often the `kuberlr` wrapper, which forks the real kubectl as a child —
// cancelling the context would otherwise kill only the wrapper and orphan the
// port-forward.
func groupKill(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
}

// LogHub is a small ring buffer with fan-out to live subscribers.
type LogHub struct {
	mu    sync.Mutex
	lines []string
	subs  map[chan string]struct{}
}

func newLogHub() *LogHub { return &LogHub{subs: make(map[chan string]struct{})} }

// Emit appends a line and fans it out to subscribers (non-blocking).
func (h *LogHub) Emit(line string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.lines = append(h.lines, line)
	if len(h.lines) > 2000 {
		h.lines = h.lines[len(h.lines)-2000:]
	}
	for ch := range h.subs {
		select {
		case ch <- line:
		default: // slow subscriber; drop
		}
	}
}

// Subscribe returns the history so far, a live channel, and a cancel func.
func (h *LogHub) Subscribe() ([]string, <-chan string, func()) {
	h.mu.Lock()
	defer h.mu.Unlock()
	hist := make([]string, len(h.lines))
	copy(hist, h.lines)
	ch := make(chan string, 256)
	h.subs[ch] = struct{}{}
	return hist, ch, func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		if _, ok := h.subs[ch]; ok {
			delete(h.subs, ch)
			close(ch)
		}
	}
}

// Session is one blueprint's running set of processes.
type Session struct {
	Blueprint string    `json:"blueprint"`
	Namespace string    `json:"namespace"`
	URL       string    `json:"url"`
	Running   bool      `json:"running"`
	StartedAt time.Time `json:"startedAt"`

	hub    *LogHub
	cancel context.CancelFunc
	mu     sync.Mutex
}

// Hub exposes the session's log hub.
func (s *Session) Hub() *LogHub { return s.hub }

// Manager owns all sessions.
type Manager struct {
	cacheDir string
	mu       sync.Mutex
	sessions map[string]*Session
}

// New returns a Manager storing venvs under cacheDir/venvs.
func New(cacheDir string) *Manager {
	return &Manager{cacheDir: cacheDir, sessions: make(map[string]*Session)}
}

// Get returns the current session for a blueprint, if any.
func (m *Manager) Get(id string) *Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sessions[id]
}

// List returns all sessions.
func (m *Manager) List() []*Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]*Session, 0, len(m.sessions))
	for _, s := range m.sessions {
		out = append(out, s)
	}
	return out
}

// StartFrontend (re)starts a blueprint's local frontend: venv + install +
// port-forwards + uvicorn. It blocks through venv setup and initial launch,
// streaming to emit, and returns once the frontend is listening (or on error).
// The port-forwards + uvicorn keep running until Stop.
func (m *Manager) StartFrontend(bp catalog.Blueprint, kubeCtx, namespace string, emit func(string)) (*Session, error) {
	lf := bp.LocalFrontend
	if lf == nil {
		return nil, fmt.Errorf("blueprint %q has no localFrontend", bp.ID)
	}
	if kubeCtx == "" {
		return nil, fmt.Errorf("no target cluster selected")
	}
	if namespace == "" {
		return nil, fmt.Errorf("namespace is required")
	}
	if _, err := exec.LookPath("python3"); err != nil {
		return nil, fmt.Errorf("python3 not found on PATH (required to run the frontend)")
	}

	// Replace any existing session.
	m.Stop(bp.ID)

	hub := newLogHub()
	log := func(s string) {
		hub.Emit(s)
		if emit != nil {
			emit(s)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	sess := &Session{
		Blueprint: bp.ID,
		Namespace: namespace,
		Running:   true,
		StartedAt: time.Now(),
		hub:       hub,
		cancel:    cancel,
	}

	frontendDir := filepath.Join(bp.Dir, lf.Dir)
	venvDir := filepath.Join(m.cacheDir, "venvs", bp.ID)

	// 1. venv (created once, reused).
	if _, err := os.Stat(filepath.Join(venvDir, "bin", "python")); err != nil {
		log("Creating virtualenv " + venvDir)
		if err := runBlocking(ctx, bp.Dir, nil, log, "python3", "-m", "venv", venvDir); err != nil {
			cancel()
			return nil, fmt.Errorf("create venv: %w", err)
		}
	}
	pip := filepath.Join(venvDir, "bin", "pip")

	// 2. install steps (relative to the blueprint root).
	for _, step := range lf.Install {
		args := append([]string{"install"}, strings.Fields(step)...)
		log("$ pip " + strings.Join(args, " "))
		if err := runBlocking(ctx, bp.Dir, nil, log, pip, args...); err != nil {
			cancel()
			return nil, fmt.Errorf("pip install %q: %w", step, err)
		}
	}

	// 3. port-forwards (supervised — kubectl port-forward exits on a connection
	// reset / pod restart and does NOT reconnect, so we restart it for the life
	// of the session).
	for _, pf := range lf.PortForwards {
		spec := fmt.Sprintf("%d:%d", pf.Local, pf.Remote)
		log(fmt.Sprintf("port-forward %s svc/%s %s (ns %s) [supervised]", pf.Name, pf.Service, spec, namespace))
		go supervisePortForward(ctx, hub, kubeCtx, namespace, pf)
	}
	// Give the forwards a moment to establish before the readiness check.
	time.Sleep(2 * time.Second)

	// 4. uvicorn (long-running).
	port := lf.Port
	if port == 0 {
		port = 8000
	}
	uvicorn := filepath.Join(venvDir, "bin", "uvicorn")
	entry := lf.Entry
	if entry == "" {
		entry = "app.main:app"
	}
	log(fmt.Sprintf("Starting uvicorn %s on :%d", entry, port))
	env := os.Environ()
	for k, v := range lf.Env {
		env = append(env, k+"="+v)
	}
	ucmd := exec.CommandContext(ctx, uvicorn, entry, "--host", "127.0.0.1", "--port", fmt.Sprint(port))
	ucmd.Dir = frontendDir
	ucmd.Env = env
	groupKill(ucmd)
	pipeToHub(ucmd, hub, "[uvicorn] ")
	if err := ucmd.Start(); err != nil {
		cancel()
		return nil, fmt.Errorf("start uvicorn: %w", err)
	}

	// 5. wait until listening.
	url := fmt.Sprintf("http://127.0.0.1:%d%s", port, lf.OpenPath)
	if waitListening(ctx, port, 90*time.Second) {
		log("Frontend is up at " + url)
	} else {
		log("Frontend did not become ready in time (still starting?) — try " + url)
	}
	sess.URL = url

	m.mu.Lock()
	m.sessions[bp.ID] = sess
	m.mu.Unlock()
	return sess, nil
}

// Stop terminates a blueprint's session (all its processes) if running.
func (m *Manager) Stop(id string) {
	m.mu.Lock()
	sess := m.sessions[id]
	delete(m.sessions, id)
	m.mu.Unlock()
	if sess == nil {
		return
	}
	sess.mu.Lock()
	sess.Running = false
	if sess.cancel != nil {
		sess.cancel()
	}
	sess.mu.Unlock()
}

// StopAll terminates every session (used on shutdown).
func (m *Manager) StopAll() {
	m.mu.Lock()
	ids := make([]string, 0, len(m.sessions))
	for id := range m.sessions {
		ids = append(ids, id)
	}
	m.mu.Unlock()
	for _, id := range ids {
		m.Stop(id)
	}
}

// runBlocking runs a command to completion, streaming combined output to log.
func runBlocking(ctx context.Context, dir string, env []string, log func(string), name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	if env != nil {
		cmd.Env = env
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		return err
	}
	scan := bufio.NewScanner(stdout)
	scan.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scan.Scan() {
		log(scan.Text())
	}
	return cmd.Wait()
}

// supervisePortForward keeps a `kubectl port-forward` alive for the life of the
// session context, restarting it (with a short backoff) whenever it exits —
// kubectl port-forward does not reconnect on its own after a dropped connection
// or a target pod restart.
func supervisePortForward(ctx context.Context, hub *LogHub, kubeCtx, namespace string, pf catalog.PortForward) {
	spec := fmt.Sprintf("%d:%d", pf.Local, pf.Remote)
	for ctx.Err() == nil {
		cmd := exec.CommandContext(ctx, "kubectl", "--context", kubeCtx, "-n", namespace,
			"port-forward", "svc/"+pf.Service, spec)
		groupKill(cmd)
		pipeToHub(cmd, hub, "["+pf.Name+"] ")
		if err := cmd.Start(); err != nil {
			hub.Emit(fmt.Sprintf("[%s] failed to start port-forward: %v", pf.Name, err))
		} else {
			cmd.Wait()
		}
		if ctx.Err() != nil {
			return
		}
		hub.Emit(fmt.Sprintf("[%s] port-forward exited; restarting…", pf.Name))
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

// pipeToHub wires a long-running command's combined output into a log hub.
func pipeToHub(cmd *exec.Cmd, hub *LogHub, prefix string) {
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return
	}
	cmd.Stderr = cmd.Stdout
	go func() {
		scan := bufio.NewScanner(stdout)
		scan.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		for scan.Scan() {
			hub.Emit(prefix + scan.Text())
		}
	}()
}

// waitListening polls a local TCP port until it accepts connections.
func waitListening(ctx context.Context, port int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return false
		default:
		}
		conn, err := net.DialTimeout("tcp", addr, time.Second)
		if err == nil {
			conn.Close()
			return true
		}
		time.Sleep(time.Second)
	}
	return false
}
