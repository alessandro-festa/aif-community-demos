// Package rancher is a tiny client for the Rancher v3 API — just enough to list
// downstream clusters and generate a kubeconfig for one. It is the only outbound
// HTTP client in the project; everything else talks to clusters via kubectl.
package rancher

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Client talks to a single Rancher server with a bearer API token.
type Client struct {
	BaseURL string // e.g. https://rancher.example.com (no trailing slash)
	Token   string // Rancher API key used as "Authorization: Bearer <token>"
	HTTP    *http.Client
}

// Cluster is a downstream cluster managed by Rancher.
type Cluster struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// New builds a client. When insecure is true, TLS certificate verification is
// skipped — Rancher servers frequently use self-signed certificates.
func New(url, token string, insecure bool) *Client {
	tr := &http.Transport{}
	if insecure {
		tr.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // user opt-in for self-signed Rancher
	}
	return &Client{
		BaseURL: strings.TrimRight(strings.TrimSpace(url), "/"),
		Token:   strings.TrimSpace(token),
		HTTP:    &http.Client{Timeout: 20 * time.Second, Transport: tr},
	}
}

func (c *Client) do(ctx context.Context, method, path string, out any) error {
	if c.BaseURL == "" {
		return fmt.Errorf("Rancher URL is required")
	}
	if c.Token == "" {
		return fmt.Errorf("Rancher API token is required")
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Accept", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("cannot reach Rancher at %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Rancher API %s: %s", resp.Status, rancherMessage(body))
	}
	if out != nil {
		if err := json.Unmarshal(body, out); err != nil {
			return fmt.Errorf("unexpected Rancher response: %w", err)
		}
	}
	return nil
}

// rancherMessage pulls the human-readable "message" out of a Rancher error body,
// falling back to the raw (truncated) body.
func rancherMessage(body []byte) string {
	var e struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(body, &e) == nil && strings.TrimSpace(e.Message) != "" {
		return e.Message
	}
	s := strings.TrimSpace(string(body))
	if len(s) > 300 {
		s = s[:300] + "…"
	}
	if s == "" {
		return "no response body"
	}
	return s
}

// ListClusters returns the downstream clusters visible to the token.
func (c *Client) ListClusters(ctx context.Context) ([]Cluster, error) {
	var out struct {
		Data []Cluster `json:"data"`
	}
	if err := c.do(ctx, http.MethodGet, "/v3/clusters", &out); err != nil {
		return nil, err
	}
	return out.Data, nil
}

// GenerateKubeconfig returns the kubeconfig YAML for a downstream cluster. The
// resulting config proxies through the Rancher server.
func (c *Client) GenerateKubeconfig(ctx context.Context, clusterID string) (string, error) {
	var out struct {
		Config string `json:"config"`
	}
	if err := c.do(ctx, http.MethodPost, "/v3/clusters/"+clusterID+"?action=generateKubeconfig", &out); err != nil {
		return "", err
	}
	if strings.TrimSpace(out.Config) == "" {
		return "", fmt.Errorf("Rancher returned an empty kubeconfig for cluster %s", clusterID)
	}
	return out.Config, nil
}
