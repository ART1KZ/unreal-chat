package main

import (
	"context"
	"encoding/json/v2"
	"github.com/unreallabsai/unreal-agent/harness/llm"
	"github.com/unreallabsai/unreal-agent/harness/llm/clients/openai"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNativeWindowReplaysOpaqueAndAllReturnedItems(t *testing.T) {
	var wire map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/responses/compact" {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"output":[{"type":"message","role":"user","content":"KEEP NATIVE USER"},{"type":"compaction","encrypted_content":"opaque-test"}],"usage":{"input_tokens":30,"output_tokens":2}}`))
			return
		}
		if err := json.UnmarshalRead(r.Body, &wire); err != nil {
			t.Error(err)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Write([]byte("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"test\",\"status\":\"completed\",\"output\":[],\"usage\":{}}}\n\n"))
	}))
	defer server.Close()
	t.Setenv("UNREAL_HARNESS_LLM_PROVIDER", "openai")
	t.Setenv("UNREAL_HARNESS_LLM_BASE_URL", server.URL)
	t.Setenv("UNREAL_HARNESS_LLM_API_KEY", "test-key")
	client, err := openai.NewClient(openai.Config{APIKey: "test-key", BaseURL: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	a := newCompactingAdapter(client, contextConfig{Window: 50000, Native: true}, filepath.Join(t.TempDir(), "checkpoint.json"), nil)
	a.forceCompact()
	if _, err = a.Respond(context.Background(), compactHistory(), llm.RequestOptions{}); err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(wire)
	if !strings.Contains(string(encoded), `"type":"compaction"`) || !strings.Contains(string(encoded), "opaque-test") || !strings.Contains(string(encoded), "KEEP NATIVE USER") {
		t.Fatalf("native window corrupted: %s", encoded)
	}
	if !a.checkpoint.Native {
		t.Fatal("native state not persisted")
	}
}

func TestNativeAccountChangeInvalidatesProjection(t *testing.T) {
	path := filepath.Join(t.TempDir(), "auth.json")
	t.Setenv("OPENAI_CODEX_AUTH_FILE", path)
	os.WriteFile(path, []byte(`{"tokens":{"account_id":"account-a"}}`), 0600)
	r := compactHistory()
	_, raw := history(r)
	a := newCompactingAdapter(&compactFake{}, contextConfig{}, filepath.Join(t.TempDir(), "checkpoint.json"), nil)
	a.checkpoint = contextCheckpoint{Version: 1, Cut: len(raw) - 1, Digest: digest(raw[:len(raw)-1]), Native: true, Model: r.Model.ID, Route: routeKey(), Items: []llm.Item{}}
	if len(a.projection(r).Input) >= len(r.Input) {
		t.Fatal("checkpoint did not apply")
	}
	os.WriteFile(path, []byte(`{"tokens":{"account_id":"account-b"}}`), 0600)
	if len(a.projection(r).Input) != len(r.Input) {
		t.Fatal("opaque state leaked between accounts")
	}
}
