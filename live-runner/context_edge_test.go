package main

import (
	"context"
	"encoding/json/jsontext"
	"errors"
	"github.com/unreallabsai/unreal-agent/harness/llm"
	"github.com/unreallabsai/unreal-agent/harness/llm/responsesapi"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNativeFallbackUsesRawHistoryNotEncryptedText(t *testing.T) {
	t.Setenv("UNREAL_HARNESS_LLM_PROVIDER", "openrouter")
	f := &compactFake{}
	a := newCompactingAdapter(f, contextConfig{Window: 30000, Native: true}, filepath.Join(t.TempDir(), "checkpoint.json"), nil)
	r := compactHistory()
	_, raw := history(r)
	a.checkpoint = contextCheckpoint{Version: 1, Cut: len(raw) - 1, Digest: digest(raw[:len(raw)-1]), Model: r.Model.ID, Route: routeKey(), Native: true, Items: []llm.Item{{Type: llm.ItemReasoning, Data: llm.Reasoning{Raw: jsontext.Value(`{"type":"compaction","encrypted_content":"unreadable-test"}`)}}}}
	a.forceCompact()
	if _, err := a.Respond(context.Background(), r, llm.RequestOptions{}); err != nil {
		t.Fatal(err)
	}
	payload := fmtItems(f.calls[0].Input)
	if strings.Contains(payload, "unreadable-test") || !strings.Contains(payload, "past past") {
		t.Fatal("fallback did not rebuild readable history")
	}
	if a.checkpoint.Native {
		t.Fatal("portable checkpoint was not saved")
	}
}

func TestArchivedToolOutputPrivateAndTranscriptUnchanged(t *testing.T) {
	path := filepath.Join(t.TempDir(), "checkpoint.json")
	a := newCompactingAdapter(&compactFake{}, contextConfig{Window: 3000, Keep: 10}, path, nil)
	text := strings.Repeat("large output line\n", 2000)
	r := llm.Request{Input: []llm.Item{
		{Type: llm.ItemMessage, Data: llm.Message{Role: llm.RoleUser, Text: "old"}},
		{Type: llm.ItemToolCall, Data: llm.ToolCall{CallID: "a", Name: "Bash"}},
		{Type: llm.ItemToolResult, Data: llm.ToolResult{CallID: "a", Output: []llm.ToolResultOutput{{Kind: llm.ToolResultText, Value: text}}}},
		{Type: llm.ItemMessage, Data: llm.Message{Role: llm.RoleUser, Text: strings.Repeat("LATEST ", 100)}},
	}}
	projected, saved, err := a.archive(r)
	if err != nil {
		t.Fatal(err)
	}
	if saved <= 0 || !strings.Contains(fmtItems(projected.Input), "[Archived tool output:") {
		t.Fatal("output not archived")
	}
	if r.Input[2].Data.(llm.ToolResult).Output[0].Value != text {
		t.Fatal("transcript mutated")
	}
	files, _ := filepath.Glob(filepath.Join(filepath.Dir(path), "archive", "*.txt"))
	if len(files) != 1 {
		t.Fatal("archive missing")
	}
	data, _ := os.ReadFile(files[0])
	if string(data) != text {
		t.Fatal("archive is incomplete")
	}
	info, _ := os.Stat(files[0])
	if info.Mode().Perm() != 0600 {
		t.Fatal("archive permissions are not private")
	}
}

type overflowingAdapter struct{ calls int }

func (a *overflowingAdapter) Respond(ctx context.Context, r llm.Request, o llm.RequestOptions) (llm.Response, error) {
	a.calls++
	if strings.Contains(fmtItems(r.Input), "Produce a compact continuation checkpoint") {
		return (&compactFake{}).Respond(ctx, r, o)
	}
	return llm.Response{}, &responsesapi.APIError{StatusCode: 400, Code: "context_length_exceeded"}
}
func TestOverflowRecoveryIsBounded(t *testing.T) {
	f := &overflowingAdapter{}
	a := newCompactingAdapter(f, contextConfig{Window: 30000}, filepath.Join(t.TempDir(), "checkpoint.json"), nil)
	_, err := a.Respond(context.Background(), compactHistory(), llm.RequestOptions{})
	var api *responsesapi.APIError
	if !errors.As(err, &api) || f.calls != 3 {
		t.Fatalf("retry not bounded: calls %d error %v", f.calls, err)
	}
}

func TestCanceledSummaryDoesNotPublishCheckpoint(t *testing.T) {
	path := filepath.Join(t.TempDir(), "checkpoint.json")
	a := newCompactingAdapter(&compactFake{}, contextConfig{Window: 30000}, path, nil)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := a.compact(ctx, compactHistory(), true); !errors.Is(err, context.Canceled) {
		t.Fatalf("want cancel, got %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("canceled checkpoint published")
	}
}
