package server

import (
	"fmt"
	"os"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/suse/blueprint-marketplace/internal/catalog"
)

// deepMerge merges src into dst in place: nested maps merge recursively, slices
// are appended, and any other value overwrites. This lets import-wizard options
// contribute list entries (e.g. proxy_config.guardrails, litellm_settings.callbacks)
// without clobbering each other.
func deepMerge(dst, src map[string]any) {
	for k, sv := range src {
		if dv, ok := dst[k]; ok {
			dm, dIsMap := asMap(dv)
			sm, sIsMap := asMap(sv)
			if dIsMap && sIsMap {
				deepMerge(dm, sm)
				dst[k] = dm
				continue
			}
			ds, dIsSlice := dv.([]any)
			ss, sIsSlice := sv.([]any)
			if dIsSlice && sIsSlice {
				dst[k] = append(ds, ss...)
				continue
			}
		}
		dst[k] = sv
	}
}

// asMap normalises the two map shapes YAML decoding can produce
// (map[string]any and map[any]any) into map[string]any.
func asMap(v any) (map[string]any, bool) {
	switch m := v.(type) {
	case map[string]any:
		return m, true
	case map[any]any:
		out := make(map[string]any, len(m))
		for k, val := range m {
			out[fmt.Sprintf("%v", k)] = val
		}
		return out, true
	default:
		return nil, false
	}
}

// injectIntoComponent deep-merges patch into the .values of the spec.components
// entry whose chartName == chartName.
func injectIntoComponent(cr map[string]any, chartName string, patch map[string]any) error {
	spec, ok := asMap(cr["spec"])
	if !ok {
		return fmt.Errorf("blueprint CR has no spec map")
	}
	comps, ok := spec["components"].([]any)
	if !ok {
		return fmt.Errorf("blueprint CR spec.components is not a list")
	}
	for i, c := range comps {
		cm, ok := asMap(c)
		if !ok {
			continue
		}
		if fmt.Sprintf("%v", cm["chartName"]) != chartName {
			continue
		}
		values, ok := asMap(cm["values"])
		if !ok {
			values = map[string]any{}
		}
		deepMerge(values, patch)
		cm["values"] = values
		comps[i] = cm
		spec["components"] = comps
		cr["spec"] = spec
		return nil
	}
	return fmt.Errorf("no component with chartName %q in blueprint CR", chartName)
}

// substituteValue returns a deep copy of m with every "{{value}}" occurrence in any
// string (recursing into nested maps and slices) replaced by val. This lets a
// wizard input's Inject template carry a placeholder that becomes the entered value.
func substituteValue(v any, val string) any {
	switch t := v.(type) {
	case string:
		return strings.ReplaceAll(t, "{{value}}", val)
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, sv := range t {
			out[k] = substituteValue(sv, val)
		}
		return out
	case map[any]any:
		out := make(map[string]any, len(t))
		for k, sv := range t {
			out[fmt.Sprintf("%v", k)] = substituteValue(sv, val)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, sv := range t {
			out[i] = substituteValue(sv, val)
		}
		return out
	default:
		return v
	}
}

// buildImportManifest reads the blueprint's CR file and merges the import wizard's
// selected options and filled inputs into the wizard's target component. Options
// contribute their Inject block verbatim; inputs contribute their Inject block with
// the "{{value}}" placeholder replaced by the entered value. It returns the rendered
// YAML to apply. With no wizard or nothing selected/entered, the raw CR is returned.
func buildImportManifest(bp catalog.Blueprint, selections []string, inputs map[string]string) ([]byte, error) {
	crPath := bp.Dir + "/" + bp.BlueprintFile
	raw, err := os.ReadFile(crPath)
	if err != nil {
		return nil, fmt.Errorf("read blueprint CR: %w", err)
	}
	if bp.ImportWizard == nil || (len(selections) == 0 && len(inputs) == 0) {
		return raw, nil
	}
	var cr map[string]any
	if err := yaml.Unmarshal(raw, &cr); err != nil {
		return nil, fmt.Errorf("parse blueprint CR: %w", err)
	}
	for _, id := range selections {
		opt, ok := bp.ImportWizard.Option(id)
		if !ok {
			return nil, fmt.Errorf("unknown wizard option %q", id)
		}
		if len(opt.Inject) == 0 {
			continue
		}
		if err := injectIntoComponent(cr, bp.ImportWizard.TargetComponent, opt.Inject); err != nil {
			return nil, err
		}
	}
	for id, val := range inputs {
		in, ok := bp.ImportWizard.Input(id)
		if !ok {
			return nil, fmt.Errorf("unknown wizard input %q", id)
		}
		if val == "" || len(in.Inject) == 0 {
			continue
		}
		patch, _ := asMap(substituteValue(in.Inject, val))
		if err := injectIntoComponent(cr, bp.ImportWizard.TargetComponent, patch); err != nil {
			return nil, err
		}
	}
	out, err := yaml.Marshal(cr)
	if err != nil {
		return nil, fmt.Errorf("render blueprint CR: %w", err)
	}
	// Placeholder replacements run on the rendered YAML so an input can fill a
	// value inside a list item (e.g. a per-model hf_token) that a deep-merge into
	// map values cannot reach.
	for id, val := range inputs {
		in, ok := bp.ImportWizard.Input(id)
		if !ok || val == "" || in.Replace == "" {
			continue
		}
		out = []byte(strings.ReplaceAll(string(out), in.Replace, val))
	}
	return out, nil
}
