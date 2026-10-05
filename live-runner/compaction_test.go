package main

import (
	"context"
	"errors"
	"github.com/unreallabsai/unreal-agent/harness/llm"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type compactFake struct {
	calls []llm.Request
	fail  bool
}

func (f *compactFake) Respond(ctx context.Context, r llm.Request, o llm.RequestOptions) (llm.Response, error) {
	f.calls = append(f.calls, r)
	if f.fail {
		return llm.Response{}, errors.New("unavailable")
	}
	return llm.Response{Output: []llm.Item{{Type: llm.ItemMessage, Data: llm.Message{Role: llm.RoleAssistant, Text: "Goal: preserve task. Decisions: fixed. Next: continue."}}}, Usage: llm.Usage{InputTokens: 100, OutputTokens: 20}}, nil
}
func compactHistory() llm.Request {
	r := llm.Request{Model: llm.Model{ID: "mock"}}
	r.Input = append(r.Input, llm.Item{Type: llm.ItemMessage, Data: llm.Message{Role: llm.RoleSystem, Text: "SYSTEM KEEP"}})
	for i := 0; i < 12; i++ {
		r.Input = append(r.Input, llm.Item{Type: llm.ItemMessage, Data: llm.Message{Role: llm.RoleUser, Text: strings.Repeat("past ", 300)}})
	}
	r.Input = append(r.Input, llm.Item{Type: llm.ItemMessage, Data: llm.Message{Role: llm.RoleUser, Text: "LATEST USER KEEP"}})
	return r
}
func TestCompactionPersistsProjectionNotTranscript(t *testing.T) {
	f := &compactFake{}
	path := filepath.Join(t.TempDir(), "checkpoint.json")
	a := newCompactingAdapter(f, contextConfig{Window: 3000, Auto: true}, path, nil)
	r := compactHistory()
	original := len(r.Input)
	if _, err := a.Respond(context.Background(), r, llm.RequestOptions{}); err != nil {
		t.Fatal(err)
	}
	if len(f.calls) < 2 {
		t.Fatalf("want summary + response, got %d", len(f.calls))
	}
	sent := f.calls[len(f.calls)-1]
	for _, request := range f.calls[:len(f.calls)-1] {
		if estimate(request) >= 3000 {
			t.Fatal("summary request exceeded window")
		}
	}
	if len(sent.Input) >= original {
		t.Fatal("context did not shrink")
	}
	if len(r.Input) != original {
		t.Fatal("canonical input mutated")
	}
	data, _ := os.ReadFile(path)
	if !strings.Contains(string(data), "Goal:") {
		t.Fatal("checkpoint absent")
	}
	joined := fmtItems(sent.Input)
	if !strings.Contains(joined, "SYSTEM KEEP") || !strings.Contains(joined, "LATEST USER KEEP") {
		t.Fatal("instructions lost")
	}
	f2 := &compactFake{}
	a2 := newCompactingAdapter(f2, contextConfig{Window: 3000, Auto: false}, path, nil)
	if _, err := a2.Respond(context.Background(), r, llm.RequestOptions{}); err != nil {
		t.Fatal(err)
	}
	if len(f2.calls[0].Input) >= original {
		t.Fatal("checkpoint not restored")
	}
}
func TestFailedSummaryLeavesCheckpointUnchanged(t *testing.T) {
	f := &compactFake{fail: true}
	path := filepath.Join(t.TempDir(), "checkpoint.json")
	a := newCompactingAdapter(f, contextConfig{Window: 3000, Auto: true}, path, nil)
	if _, err := a.Respond(context.Background(), compactHistory(), llm.RequestOptions{}); err == nil {
		t.Fatal("failure hidden")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("failed checkpoint published")
	}
}
func TestCutNeverSplitsOpenToolCalls(t *testing.T) {
	items := []llm.Item{{Type: llm.ItemMessage, Data: llm.Message{Role: llm.RoleUser, Text: "task"}}, {Type: llm.ItemToolCall, Data: llm.ToolCall{CallID: "a"}}, {Type: llm.ItemToolResult, Data: llm.ToolResult{CallID: "a"}}, {Type: llm.ItemToolCall, Data: llm.ToolCall{CallID: "pending"}}}
	for _, cut := range safeCuts(items) {
		if cut == 2 || cut == 4 {
			t.Fatalf("unsafe cut %d", cut)
		}
	}
}
