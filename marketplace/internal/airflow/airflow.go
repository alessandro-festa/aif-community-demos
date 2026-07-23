// Package airflow is a tiny client for the Airflow 3 REST API (v2) — just enough
// to trigger DAGs and poll their run state so bpm can run a blueprint's DAG
// pipeline sequentially. Modeled on internal/rancher (the project's other
// outbound HTTP client); everything else talks to clusters via kubectl.
//
// Airflow 3 auth is a JWT obtained from POST /auth/token with the web credentials
// (chart default admin/admin), then sent as "Authorization: Bearer <token>" to
// /api/v2. git-synced DAGs default to PAUSED, so a DAG is unpaused before it is
// triggered or the run would sit queued forever.
package airflow

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Client talks to a single Airflow API server.
type Client struct {
	BaseURL  string // e.g. http://127.0.0.1:34567 (no trailing slash)
	User     string
	Password string
	HTTP     *http.Client

	token string // cached JWT; refreshed on 401
}

// New builds a client pointed at an Airflow API server base URL.
func New(baseURL, user, password string) *Client {
	return &Client{
		BaseURL:  strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		User:     user,
		Password: password,
		HTTP:     &http.Client{Timeout: 30 * time.Second},
	}
}

// authToken fetches (and caches) a JWT via POST /auth/token.
func (c *Client) authToken(ctx context.Context) (string, error) {
	if c.token != "" {
		return c.token, nil
	}
	body, _ := json.Marshal(map[string]string{"username": c.User, "password": c.Password})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/auth/token", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return "", fmt.Errorf("cannot reach Airflow at %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("Airflow /auth/token %s: %s", resp.Status, message(raw))
	}
	var out struct {
		AccessToken string `json:"access_token"`
		JWTToken    string `json:"jwt_token"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", fmt.Errorf("unexpected Airflow token response: %w", err)
	}
	c.token = out.AccessToken
	if c.token == "" {
		c.token = out.JWTToken
	}
	if c.token == "" {
		return "", fmt.Errorf("Airflow returned an empty token")
	}
	return c.token, nil
}

// do issues an authenticated /api/v2 request, refreshing the token once on 401.
// in (if non-nil) is JSON-encoded as the body; out (if non-nil) is decoded from
// the response.
func (c *Client) do(ctx context.Context, method, path string, in, out any) error {
	call := func() (*http.Response, []byte, error) {
		tok, err := c.authToken(ctx)
		if err != nil {
			return nil, nil, err
		}
		var rdr io.Reader
		if in != nil {
			b, _ := json.Marshal(in)
			rdr = bytes.NewReader(b)
		}
		req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+"/api/v2"+path, rdr)
		if err != nil {
			return nil, nil, err
		}
		req.Header.Set("Authorization", "Bearer "+tok)
		req.Header.Set("Accept", "application/json")
		if in != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := c.HTTP.Do(req)
		if err != nil {
			return nil, nil, fmt.Errorf("cannot reach Airflow at %s: %w", c.BaseURL, err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		return resp, body, nil
	}

	resp, body, err := call()
	if err != nil {
		return err
	}
	if resp.StatusCode == http.StatusUnauthorized { // token expired — refresh once
		c.token = ""
		resp, body, err = call()
		if err != nil {
			return err
		}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Airflow API %s %s: %s", method, path, message(body))
	}
	if out != nil && len(body) > 0 {
		if err := json.Unmarshal(body, out); err != nil {
			return fmt.Errorf("unexpected Airflow response: %w", err)
		}
	}
	return nil
}

// Unpause clears a DAG's is_paused flag (git-synced DAGs default to paused).
func (c *Client) Unpause(ctx context.Context, dagID string) error {
	return c.do(ctx, http.MethodPatch, "/dags/"+dagID+"?update_mask=is_paused",
		map[string]any{"is_paused": false}, nil)
}

// Trigger starts a new run of the DAG and returns its dag_run_id.
func (c *Client) Trigger(ctx context.Context, dagID string) (string, error) {
	var out struct {
		DagRunID string `json:"dag_run_id"`
	}
	if err := c.do(ctx, http.MethodPost, "/dags/"+dagID+"/dagRuns",
		map[string]any{"logical_date": nil}, &out); err != nil {
		return "", err
	}
	return out.DagRunID, nil
}

// RunState returns the state of a specific DAG run
// (queued | running | success | failed).
func (c *Client) RunState(ctx context.Context, dagID, runID string) (string, error) {
	var out struct {
		State string `json:"state"`
	}
	if err := c.do(ctx, http.MethodGet, "/dags/"+dagID+"/dagRuns/"+runID, nil, &out); err != nil {
		return "", err
	}
	return out.State, nil
}

// message pulls a human-readable detail out of an Airflow error body, falling
// back to the raw (truncated) body.
func message(body []byte) string {
	var e struct {
		Detail string `json:"detail"`
		Title  string `json:"title"`
	}
	if json.Unmarshal(body, &e) == nil {
		if strings.TrimSpace(e.Detail) != "" {
			return e.Detail
		}
		if strings.TrimSpace(e.Title) != "" {
			return e.Title
		}
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
