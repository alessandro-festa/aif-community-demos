// Command marketplace is a local, single-binary SUSE-styled launcher for SUSE
// AI Factory blueprints. It pulls blueprint content (including each blueprint's
// local frontend) from a git repo, lists them in a catalog, imports a selected
// Blueprint CR into a chosen cluster via kubectl, and walks the user through a
// guided demo — starting local frontends + port-forwards as needed.
package main

import (
	"context"
	"embed"
	"flag"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/suse/blueprint-marketplace/internal/catalog"
	"github.com/suse/blueprint-marketplace/internal/config"
	"github.com/suse/blueprint-marketplace/internal/gitrepo"
	"github.com/suse/blueprint-marketplace/internal/proc"
	"github.com/suse/blueprint-marketplace/internal/server"
)

//go:embed all:web
var webFS embed.FS

func main() {
	addr := flag.String("addr", "127.0.0.1:8900", "listen address")
	repo := flag.String("repo", "", "blueprints git repo URL (overrides saved settings)")
	ref := flag.String("ref", "", "blueprints git ref/branch (overrides saved settings)")
	kctx := flag.String("context", "", "target kube context (overrides saved settings)")
	dir := flag.String("dir", "", "use a local blueprints/ directory instead of git (dev)")
	flag.Parse()

	cfgStore, err := config.Load()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	// Apply flag overrides onto the persisted config.
	cur := cfgStore.Get()
	if *repo != "" {
		cur.BlueprintsRepo = *repo
	}
	if *ref != "" {
		cur.BlueprintsRef = *ref
	}
	if *kctx != "" {
		cur.TargetContext = *kctx
	}
	_ = cfgStore.Set(cur)

	cacheDir, err := config.Dir()
	if err != nil {
		log.Fatalf("cache dir: %v", err)
	}

	cat := catalog.New("")
	pm := proc.New(cacheDir)

	// resync pulls the git repo and reloads the catalog from it. When --dir is
	// set we skip git entirely and point the catalog at the local directory.
	resync := func() error {
		if *dir != "" {
			return cat.SetRoot(*dir)
		}
		c := cfgStore.Get()
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cancel()
		root, err := gitrepo.Sync(ctx, c.BlueprintsRepo, c.BlueprintsRef, cacheDir)
		if err != nil {
			return err
		}
		return cat.SetRoot(root)
	}

	if err := resync(); err != nil {
		log.Printf("warning: could not load blueprints yet: %v", err)
		log.Printf("set a valid repo in Settings, or restart with --dir")
	}

	// With --dir, settings changes shouldn't trigger a git pull.
	var resyncForSettings func() error
	if *dir == "" {
		resyncForSettings = resync
	}

	webRoot, err := fs.Sub(webFS, "web")
	if err != nil {
		log.Fatalf("web assets: %v", err)
	}
	srv := server.New(cfgStore, cat, pm, webRoot, resyncForSettings)

	httpServer := &http.Server{Addr: *addr, Handler: srv.Handler()}

	// Graceful shutdown: reap all child processes (port-forwards, uvicorn).
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-stop
		log.Println("shutting down; stopping child processes…")
		pm.StopAll()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(ctx)
	}()

	log.Printf("Blueprint Marketplace on http://%s", *addr)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
	pm.StopAll()
}
