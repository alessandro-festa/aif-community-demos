package server

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/suse/blueprint-marketplace/internal/catalog"
)

func TestDeepMergeAppendsListsAndRecurses(t *testing.T) {
	dst := map[string]any{
		"proxy_config": map[string]any{
			"guardrails": []any{map[string]any{"guardrail_name": "a"}},
			"keep":       "me",
		},
	}
	src := map[string]any{
		"proxy_config": map[string]any{
			"guardrails": []any{map[string]any{"guardrail_name": "b"}},
			"new":        "value",
		},
		"envVars": map[string]any{"X": "1"},
	}
	deepMerge(dst, src)

	pc := dst["proxy_config"].(map[string]any)
	gr := pc["guardrails"].([]any)
	if len(gr) != 2 {
		t.Fatalf("expected 2 guardrails after merge, got %d", len(gr))
	}
	if pc["keep"] != "me" || pc["new"] != "value" {
		t.Fatalf("recursive map merge lost keys: %+v", pc)
	}
	if _, ok := dst["envVars"]; !ok {
		t.Fatalf("top-level key not added")
	}
}

func TestBuildImportManifestMergesSelectedOptions(t *testing.T) {
	dir := t.TempDir()
	cr := `apiVersion: ai-factory.suse.com/v1alpha1
kind: Blueprint
spec:
  components:
  - chartName: ollama
    chartRepo: application-collection
    values:
      fullnameOverride: ollama
  - chartName: litellm
    chartRepo: suse-ai-registry
    values:
      fullnameOverride: litellm
      proxy_config:
        guardrails: []
`
	crFile := "litellm-guardrails-1-0-0.yaml"
	if err := os.WriteFile(filepath.Join(dir, crFile), []byte(cr), 0o644); err != nil {
		t.Fatal(err)
	}

	bp := catalog.Blueprint{
		Dir:           dir,
		BlueprintFile: crFile,
		ImportWizard: &catalog.ImportWizard{
			TargetComponent: "litellm",
			Options: []catalog.WizardOption{
				{ID: "presidio-pii-mask", Inject: map[string]any{
					"proxy_config": map[string]any{
						"guardrails": []any{map[string]any{"guardrail_name": "presidio-pii-mask"}},
					},
				}},
				{ID: "hide-secrets", Inject: map[string]any{
					"proxy_config": map[string]any{
						"guardrails": []any{map[string]any{"guardrail_name": "hide-secrets"}},
					},
				}},
			},
		},
	}

	out, err := buildImportManifest(bp, []string{"presidio-pii-mask", "hide-secrets"}, nil)
	if err != nil {
		t.Fatal(err)
	}

	var got map[string]any
	if err := yaml.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	comps := got["spec"].(map[string]any)["components"].([]any)
	var litellm map[string]any
	for _, c := range comps {
		cm := c.(map[string]any)
		if cm["chartName"] == "litellm" {
			litellm = cm
		}
	}
	if litellm == nil {
		t.Fatal("litellm component missing")
	}
	gr := litellm["values"].(map[string]any)["proxy_config"].(map[string]any)["guardrails"].([]any)
	if len(gr) != 2 {
		t.Fatalf("expected 2 injected guardrails, got %d: %+v", len(gr), gr)
	}
	names := map[string]bool{}
	for _, g := range gr {
		names[g.(map[string]any)["guardrail_name"].(string)] = true
	}
	if !names["presidio-pii-mask"] || !names["hide-secrets"] {
		t.Fatalf("missing expected guardrail names: %+v", names)
	}
}

func TestBuildImportManifestNoSelectionsReturnsRaw(t *testing.T) {
	dir := t.TempDir()
	cr := "kind: Blueprint\nspec: {}\n"
	crFile := "bp.yaml"
	if err := os.WriteFile(filepath.Join(dir, crFile), []byte(cr), 0o644); err != nil {
		t.Fatal(err)
	}
	bp := catalog.Blueprint{Dir: dir, BlueprintFile: crFile}
	out, err := buildImportManifest(bp, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != cr {
		t.Fatalf("expected raw file unchanged, got:\n%s", out)
	}
}

func TestBuildImportManifestSubstitutesInputValue(t *testing.T) {
	dir := t.TempDir()
	cr := `apiVersion: ai-factory.suse.com/v1alpha1
kind: Blueprint
spec:
  components:
  - chartName: vllm
    chartRepo: application-collection
    values:
      servingEngineSpec:
        modelSpec:
        - name: medgemma
`
	crFile := "xray-copilot-vllm-1-0-0.yaml"
	if err := os.WriteFile(filepath.Join(dir, crFile), []byte(cr), 0o644); err != nil {
		t.Fatal(err)
	}

	bp := catalog.Blueprint{
		Dir:           dir,
		BlueprintFile: crFile,
		ImportWizard: &catalog.ImportWizard{
			TargetComponent: "vllm",
			Inputs: []catalog.WizardInput{
				{ID: "hf-token", Required: true, Secret: true, Inject: map[string]any{
					"servingEngineSpec": map[string]any{
						"env": []any{
							map[string]any{"name": "HF_TOKEN", "value": "{{value}}"},
						},
					},
				}},
			},
		},
	}

	out, err := buildImportManifest(bp, nil, map[string]string{"hf-token": "hf_secret123"})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := yaml.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	comps := got["spec"].(map[string]any)["components"].([]any)
	vals := comps[0].(map[string]any)["values"].(map[string]any)
	env := vals["servingEngineSpec"].(map[string]any)["env"].([]any)
	got0 := env[0].(map[string]any)
	if got0["name"] != "HF_TOKEN" || got0["value"] != "hf_secret123" {
		t.Fatalf("expected HF_TOKEN=hf_secret123, got %+v", got0)
	}
	// Ensure the placeholder was fully substituted (not left literal).
	if _, ok := got0["value"].(string); !ok || got0["value"] == "{{value}}" {
		t.Fatalf("placeholder not substituted: %+v", got0)
	}
}

func TestBuildImportManifestReplacesTokenInListItems(t *testing.T) {
	dir := t.TempDir()
	// Two modelSpec list entries, each with an {{HF_TOKEN}} placeholder — a
	// deep-merge can't reach inside list items, so the input uses Replace.
	cr := `apiVersion: ai-factory.suse.com/v1alpha1
kind: Blueprint
spec:
  components:
  - chartName: vllm
    values:
      servingEngineSpec:
        modelSpec:
        - name: medgemma
          hf_token: "{{HF_TOKEN}}"
        - name: llava-med
          hf_token: "{{HF_TOKEN}}"
`
	crFile := "xray-copilot-vllm-1-0-0.yaml"
	if err := os.WriteFile(filepath.Join(dir, crFile), []byte(cr), 0o644); err != nil {
		t.Fatal(err)
	}
	bp := catalog.Blueprint{
		Dir: dir, BlueprintFile: crFile,
		ImportWizard: &catalog.ImportWizard{
			TargetComponent: "vllm",
			Inputs: []catalog.WizardInput{
				{ID: "hf-token", Required: true, Secret: true, Replace: "{{HF_TOKEN}}"},
			},
		},
	}
	out, err := buildImportManifest(bp, nil, map[string]string{"hf-token": "hf_abc"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(out), "{{HF_TOKEN}}") {
		t.Fatalf("placeholder left in output:\n%s", out)
	}
	if n := strings.Count(string(out), "hf_abc"); n != 2 {
		t.Fatalf("expected token in both modelSpec entries (2), got %d:\n%s", n, out)
	}
}
