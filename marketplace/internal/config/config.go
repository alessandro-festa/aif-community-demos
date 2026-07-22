// Package config loads and persists the marketplace's user settings — the
// blueprints git repo (URL + ref) and the target kube context — to a small YAML
// file under the user's home directory.
package config

import (
	"errors"
	"os"
	"path/filepath"
	"sync"

	"gopkg.in/yaml.v3"
)

// Config is the persisted user configuration, editable from the Settings page.
type Config struct {
	BlueprintsRepo string `yaml:"blueprintsRepo" json:"blueprintsRepo"`
	BlueprintsRef  string `yaml:"blueprintsRef" json:"blueprintsRef"`
	TargetContext  string `yaml:"targetContext" json:"targetContext"`
	// Kubeconfigs are extra kubeconfig files imported from the Settings page; they
	// are merged (via KUBECONFIG) with the host/default kubeconfig so their contexts
	// become selectable without launching bpm with a custom KUBECONFIG.
	Kubeconfigs []string `yaml:"kubeconfigs" json:"kubeconfigs,omitempty"`
	// RancherURL and RancherInsecure remember the last Rancher server used to import
	// downstream clusters. These are non-secret conveniences; the API token is never
	// persisted (kept in memory only, re-entered each session).
	RancherURL      string `yaml:"rancherURL" json:"rancherURL,omitempty"`
	RancherInsecure bool   `yaml:"rancherInsecure" json:"rancherInsecure,omitempty"`
}

// Defaults returns the built-in configuration used when no file exists yet.
func Defaults() Config {
	return Config{
		BlueprintsRepo: "https://github.com/alessandro-festa/aif-community-demos.git",
		BlueprintsRef:  "main",
		TargetContext:  "",
	}
}

// Store owns the config file and serializes access to the in-memory copy.
type Store struct {
	path string
	mu   sync.RWMutex
	cfg  Config
}

// Dir returns the marketplace's config/cache directory (~/.suse-bp-marketplace),
// creating it if necessary.
func Dir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(home, ".suse-bp-marketplace")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	return dir, nil
}

// Load reads the config file (or seeds it from Defaults if absent).
func Load() (*Store, error) {
	dir, err := Dir()
	if err != nil {
		return nil, err
	}
	s := &Store{path: filepath.Join(dir, "config.yaml"), cfg: Defaults()}
	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return s, nil // defaults; not yet persisted
		}
		return nil, err
	}
	if err := yaml.Unmarshal(data, &s.cfg); err != nil {
		return nil, err
	}
	return s, nil
}

// Get returns a copy of the current config.
func (s *Store) Get() Config {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.cfg
}

// Set replaces the config and persists it to disk.
func (s *Store) Set(c Config) error {
	s.mu.Lock()
	s.cfg = c
	s.mu.Unlock()
	return s.save(c)
}

func (s *Store) save(c Config) error {
	data, err := yaml.Marshal(c)
	if err != nil {
		return err
	}
	return os.WriteFile(s.path, data, 0o644)
}
