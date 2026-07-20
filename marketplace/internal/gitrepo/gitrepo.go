// Package gitrepo clones/updates the blueprints git repository into a local
// cache directory so the marketplace can read blueprint metadata and run the
// blueprints' local frontends from the checked-out source.
package gitrepo

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

// Sync ensures cacheDir contains a checkout of repo at ref and returns the path
// to the blueprints directory within it (repo root + "/blueprints").
//
// If the directory already contains a clone, it fetches and hard-resets to the
// ref; otherwise it clones. Requires `git` on PATH.
func Sync(ctx context.Context, repo, ref, cacheDir string) (string, error) {
	if repo == "" {
		return "", fmt.Errorf("no blueprints repo configured")
	}
	if ref == "" {
		ref = "main"
	}
	checkout := filepath.Join(cacheDir, "checkout")

	if _, err := os.Stat(filepath.Join(checkout, ".git")); err == nil {
		// Existing clone: fetch + reset to ref.
		if out, err := run(ctx, checkout, "git", "fetch", "--depth", "1", "origin", ref); err != nil {
			return "", fmt.Errorf("git fetch: %v: %s", err, out)
		}
		if out, err := run(ctx, checkout, "git", "reset", "--hard", "FETCH_HEAD"); err != nil {
			return "", fmt.Errorf("git reset: %v: %s", err, out)
		}
	} else {
		if err := os.MkdirAll(cacheDir, 0o755); err != nil {
			return "", err
		}
		_ = os.RemoveAll(checkout)
		if out, err := run(ctx, "", "git", "clone", "--depth", "1", "--branch", ref, repo, checkout); err != nil {
			return "", fmt.Errorf("git clone: %v: %s", err, out)
		}
	}

	blueprints := filepath.Join(checkout, "blueprints")
	if _, err := os.Stat(blueprints); err != nil {
		return "", fmt.Errorf("no 'blueprints' directory in %s@%s", repo, ref)
	}
	return blueprints, nil
}

func run(ctx context.Context, dir, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	return buf.String(), err
}
