// Package kube wraps the `kubectl` binary for the operations the marketplace
// needs: listing contexts, checking SUSE AI Factory readiness, running a
// blueprint's prerequisite checks, and applying (importing) a Blueprint CR.
package kube

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"os/exec"
	"strings"

	"github.com/suse/blueprint-marketplace/internal/catalog"
)

// aifCRD is the Blueprint CRD that indicates SUSE AI Factory is installed.
const aifCRD = "blueprints.ai-factory.suse.com"

// Context is a kube context with SUSE AI Factory readiness.
type Context struct {
	Name    string `json:"name"`
	Current bool   `json:"current"`
	Ready   bool   `json:"ready"` // AI Factory Blueprint CRD present
}

// Contexts lists kube contexts from the host kubeconfig and flags which have
// the AI Factory CRD installed.
func Contexts(ctx context.Context) ([]Context, error) {
	names, err := output(ctx, "", "kubectl", "config", "get-contexts", "-o", "name")
	if err != nil {
		return nil, fmt.Errorf("kubectl config get-contexts: %w", err)
	}
	current, _ := output(ctx, "", "kubectl", "config", "current-context")
	current = strings.TrimSpace(current)

	var out []Context
	for _, name := range strings.Fields(names) {
		out = append(out, Context{
			Name:    name,
			Current: name == current,
			Ready:   hasCRD(ctx, name, aifCRD),
		})
	}
	return out, nil
}

func hasCRD(ctx context.Context, kubeCtx, crd string) bool {
	_, err := output(ctx, kubeCtx, "kubectl", "get", "crd", crd, "-o", "name")
	return err == nil
}

// PrereqResult is the outcome of one prerequisite check.
type PrereqResult struct {
	ID      string `json:"id"`
	Label   string `json:"label"`
	OK      bool   `json:"ok"`
	Message string `json:"message"`
}

// CheckPrereqs runs every prerequisite of a blueprint against the given context.
func CheckPrereqs(ctx context.Context, kubeCtx string, reqs []catalog.Prerequisite) []PrereqResult {
	results := make([]PrereqResult, 0, len(reqs))
	for _, r := range reqs {
		ok, msg := checkOne(ctx, kubeCtx, r)
		results = append(results, PrereqResult{ID: r.ID, Label: r.Label, OK: ok, Message: msg})
	}
	return results
}

func checkOne(ctx context.Context, kubeCtx string, r catalog.Prerequisite) (bool, string) {
	if kubeCtx == "" {
		return false, "no target cluster selected"
	}
	switch r.Kind {
	case "crd":
		if hasCRD(ctx, kubeCtx, r.Name) {
			return true, "present"
		}
		return false, "not found"
	case "namespace":
		if _, err := output(ctx, kubeCtx, "kubectl", "get", "namespace", r.Name, "-o", "name"); err == nil {
			return true, "present"
		}
		return false, "not found"
	case "default-storageclass":
		out, err := output(ctx, kubeCtx, "kubectl", "get", "sc", "-o",
			`jsonpath={range .items[*]}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{" "}{.metadata.name}{"\n"}{end}`)
		if err != nil {
			return false, "could not list storageclasses"
		}
		for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
			if strings.HasPrefix(strings.TrimSpace(line), "true ") {
				return true, "default: " + strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "true "))
			}
		}
		return false, "no default StorageClass"
	default:
		return false, "unknown check kind: " + r.Kind
	}
}

// ServiceReady reports whether a Service in a namespace has ready backing
// endpoints (i.e. the component is up), and how many.
func ServiceReady(ctx context.Context, kubeCtx, namespace, service string) (bool, int) {
	if kubeCtx == "" || namespace == "" {
		return false, 0
	}
	out, err := output(ctx, kubeCtx, "kubectl", "-n", namespace, "get", "endpoints", service,
		"-o", `jsonpath={range .subsets[*]}{range .addresses[*]}{.ip}{"\n"}{end}{end}`)
	if err != nil {
		return false, 0
	}
	n := 0
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if strings.TrimSpace(line) != "" {
			n++
		}
	}
	return n > 0, n
}

// Apply streams `kubectl apply -f file` output line-by-line to emit.
func Apply(ctx context.Context, kubeCtx, file string, emit func(line string)) error {
	if kubeCtx == "" {
		return fmt.Errorf("no target cluster selected")
	}
	args := []string{"--context", kubeCtx, "apply", "-f", file}
	return stream(ctx, "kubectl", args, emit)
}

// output runs a command and returns combined stdout (used for reads).
func output(ctx context.Context, kubeCtx, name string, args ...string) (string, error) {
	if kubeCtx != "" {
		args = append([]string{"--context", kubeCtx}, args...)
	}
	cmd := exec.CommandContext(ctx, name, args...)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	return buf.String(), err
}

// stream runs a command and calls emit for each line of combined output.
func stream(ctx context.Context, name string, args []string, emit func(string)) error {
	cmd := exec.CommandContext(ctx, name, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = cmd.Stdout // merge stderr into the same pipe
	if err := cmd.Start(); err != nil {
		return err
	}
	scan := bufio.NewScanner(stdout)
	scan.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scan.Scan() {
		emit(scan.Text())
	}
	if err := scan.Err(); err != nil && err != io.EOF {
		emit("stream error: " + err.Error())
	}
	return cmd.Wait()
}
