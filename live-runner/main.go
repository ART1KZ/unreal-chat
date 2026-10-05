// uachat-live-runner is a transport adapter, not a fork of the harness.
// Scheduling, tools, recovery, context and persistence belong to unreal-agent.
package main

import (
	"bufio"
	"context"
	"encoding/json/v2"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
	"uuid"

	"github.com/unreallabsai/unreal-agent/harness/contextbuilder"
	"github.com/unreallabsai/unreal-agent/harness/coordinator"
	"github.com/unreallabsai/unreal-agent/harness/inbox"
	"github.com/unreallabsai/unreal-agent/harness/llm"
	"github.com/unreallabsai/unreal-agent/harness/llm/clients/fireworks"
	"github.com/unreallabsai/unreal-agent/harness/llm/clients/ollama"
	"github.com/unreallabsai/unreal-agent/harness/llm/clients/openai"
	"github.com/unreallabsai/unreal-agent/harness/llm/clients/openaicodex"
	"github.com/unreallabsai/unreal-agent/harness/llm/clients/openrouter"
	"github.com/unreallabsai/unreal-agent/harness/operation"
	"github.com/unreallabsai/unreal-agent/harness/session"
	"github.com/unreallabsai/unreal-agent/harness/sessionstore"
	"github.com/unreallabsai/unreal-agent/harness/sessionstore/localfile"
	"github.com/unreallabsai/unreal-agent/harness/tool"
	"github.com/unreallabsai/unreal-agent/harness/tool/bash"
	"github.com/unreallabsai/unreal-agent/harness/tool/viewimage"
)

const protocolVersion = 1
const maxTextBytes = 8 * 1024 * 1024
const defaultSystemPrompt = `You are an AI agent running inside an isolated sandbox container.

## Guidelines
- Save output files to the workspace root.
- For large datasets, inspect a sample first before processing everything.
`

type message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
	ID      string `json:"message_id"`
}
type request struct {
	Context      *contextConfig `json:"context"`
	CompactOnly  bool           `json:"compact_only"`
	Compact      bool           `json:"compact"`
	Prompt       *string        `json:"prompt"`
	Messages     []message      `json:"messages"`
	Model        string         `json:"model"`
	SessionID    string         `json:"session_id"`
	Thinking     string         `json:"thinking_level"`
	Attempts     *int           `json:"max_attempts"`
	SystemPrompt *string        `json:"system_prompt"`
	Disallowed   []string       `json:"disallowed_tools"`
	Extra        []string       `json:"extra_allowed_tools"`
	Partial      bool           `json:"include_partial_messages"`
}
type frame struct {
	Type     string   `json:"type"`
	Protocol int      `json:"protocol"`
	Request  *request `json:"request"`
	ID       string   `json:"id"`
	Text     string   `json:"text"`
	Mode     string   `json:"mode"`
	Thinking string   `json:"thinking"`
}
type output struct {
	mu     sync.Mutex
	out    io.Writer
	cancel context.CancelFunc
	err    error
	once   bool
}

func (o *output) emit(value any) { o.mu.Lock(); defer o.mu.Unlock(); o.write(value) }
func (o *output) write(value any) {
	if o.once {
		if event, ok := value.(map[string]any); ok {
			if kind, ok := event["type"].(string); ok && strings.HasPrefix(kind, "live.") {
				return
			}
		}
	}
	if o.err != nil {
		return
	}
	data, err := json.Marshal(value)
	if err == nil {
		_, err = fmt.Fprintf(o.out, "%s\n", data)
	}
	if err != nil {
		o.err = err
		o.cancel()
	}
}
func (o *output) result() error { o.mu.Lock(); defer o.mu.Unlock(); return o.err }

type client interface {
	llm.Adapter
	Close() error
}

func newClient(provider, base, key string, attempts int) (client, error) {
	switch provider {
	case "openai", "":
		if base == "" {
			base = "https://api.openai.com/v1"
		}
		if key == "" {
			key = os.Getenv("OPENAI_API_KEY")
		}
		return openai.NewClient(openai.Config{APIKey: key, BaseURL: base, MaxAttempts: &attempts})
	case "ollama":
		return ollama.NewClient(ollama.Config{BaseURL: base, MaxAttempts: &attempts})
	case "openai-codex":
		config, err := openaicodex.EnvironmentConfig(os.Getenv)
		if err != nil {
			return nil, err
		}
		if base != "" {
			config.BaseURL = base
		}
		config.MaxAttempts = &attempts
		return openaicodex.NewClient(config)
	case "openrouter":
		if base == "" {
			base = "https://openrouter.ai/api/v1"
		}
		if key == "" {
			key = os.Getenv("OPENROUTER_API_KEY")
		}
		return openrouter.NewClient(openrouter.Config{APIKey: key, BaseURL: base, MaxAttempts: &attempts})
	case "fireworks":
		if base == "" {
			base = "https://api.fireworks.ai/inference/v1"
		}
		if key == "" {
			key = os.Getenv("FIREWORKS_API_KEY")
		}
		return fireworks.NewClient(fireworks.Config{APIKey: key, BaseURL: base, MaxAttempts: &attempts})
	default:
		return nil, fmt.Errorf("unsupported provider %q", provider)
	}
}
func validateID(id string) error {
	_, err := uuid.Parse(id)
	if err != nil {
		return errors.New("input id must be UUID")
	}
	return nil
}
func validSession(id string) bool {
	return id != "" && len(id) <= 128 && id != "." && id != ".." && !strings.ContainsAny(id, "/\\") && strings.IndexFunc(id, func(r rune) bool { return r < 32 || r == 127 }) < 0
}
func validate(req *request) error {
	if req == nil {
		return errors.New("start request missing")
	}
	if !validSession(req.SessionID) {
		return errors.New("invalid session id")
	}
	if req.Thinking == "" {
		req.Thinking = "high"
	}
	if !llm.ReasoningEffort(req.Thinking).Valid() {
		return errors.New("invalid thinking level")
	}
	if req.CompactOnly {
		if req.Context == nil {
			return errors.New("manual compaction requires context configuration")
		}
		return nil
	}
	if req.Messages == nil {
		if req.Prompt == nil {
			return errors.New("prompt/messages missing")
		}
		req.Messages = []message{{Role: "user", Content: *req.Prompt, ID: uuid.New().String()}}
	}
	if len(req.Messages) == 0 {
		return errors.New("messages empty")
	}
	for i := range req.Messages {
		msg := &req.Messages[i]
		if msg.Role != "" && msg.Role != "user" {
			return errors.New("only user inputs are allowed")
		}
		if msg.Content == "" || len(msg.Content) > maxTextBytes {
			return errors.New("invalid input size")
		}
		if msg.ID == "" {
			msg.ID = uuid.New().String()
		}
		if err := validateID(msg.ID); err != nil {
			return err
		}
	}
	return nil
}
func decode(line []byte) (frame, error) {
	var f frame
	err := json.Unmarshal(line, &f, json.RejectUnknownMembers(true))
	return f, err
}
func loadEnvironment(workspace string) error {
	data, err := os.ReadFile(filepath.Join(workspace, ".env"))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, ok := strings.Cut(line, "=")
		name = strings.TrimSpace(name)
		if !ok || name == "" {
			continue
		}
		if _, exists := os.LookupEnv(name); exists && name != "SANDBOX_EGRESS_PROXY" {
			continue
		}
		if err = os.Setenv(name, strings.TrimSpace(value)); err != nil {
			return err
		}
	}
	if proxy := os.Getenv("SANDBOX_EGRESS_PROXY"); proxy != "" {
		return os.Setenv("HTTPS_PROXY", proxy)
	}
	return nil
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	out := &output{out: os.Stdout, cancel: cancel}
	err := run(ctx, out)
	if err != nil {
		out.emit(map[string]any{"type": "error", "message": err.Error()})
		if ctx.Err() != nil {
			os.Exit(130)
		}
		os.Exit(1)
	}
}
func run(parent context.Context, out *output) error {
	workspace := flag.String("workspace", ".", "tool workspace")
	dir := flag.String("session-directory", "", "same session store as the stock runner")
	parentPID := flag.Int("parent-pid", 0, "cancel when the owning client disappears")
	once := flag.Bool("once", false, "one stock-schema request on stdin; native JSONL events")
	listSkills := flag.Bool("list-skills", false, "print skill catalog and discovery diagnostics without contacting a provider")
	version := flag.Bool("version", false, "print protocol and harness version")
	flag.Parse()
	if *version {
		fmt.Println("uachat-live-runner protocol=1 harness=v0.2.0 skills=1 once=1")
		return nil
	}
	abs, err := filepath.Abs(*workspace)
	if err != nil {
		return err
	}
	info, err := os.Stat(abs)
	if err != nil || !info.IsDir() {
		return errors.New("workspace must be a directory")
	}
	out.once = *once
	if *listSkills {
		catalog := discoverSkills(abs)
		data, err := json.Marshal(catalog)
		if err != nil {
			return err
		}
		fmt.Println(string(data))
		return nil
	}
	if *dir == "" {
		root := os.Getenv("XDG_STATE_HOME")
		if root == "" {
			home, e := os.UserHomeDir()
			if e != nil {
				return e
			}
			root = filepath.Join(home, ".local", "state")
		}
		*dir = filepath.Join(root, "unreal-agent", "sessions")
	}
	if err = loadEnvironment(abs); err != nil {
		return err
	}
	scan := bufio.NewScanner(os.Stdin)
	scan.Buffer(make([]byte, 64*1024), 32*1024*1024)
	if !scan.Scan() {
		return errors.New("start frame missing")
	}
	var boot frame
	if *once {
		var initial request
		if err := json.Unmarshal(scan.Bytes(), &initial, json.RejectUnknownMembers(true)); err != nil {
			return errors.New("invalid request JSON")
		}
		if initial.SessionID == "" {
			initial.SessionID = uuid.New().String()
		}
		boot = frame{Type: "start", Protocol: protocolVersion, Request: &initial}
	} else {
		boot, err = decode(scan.Bytes())
		if err != nil {
			return errors.New("invalid start JSON")
		}
		if boot.Type != "start" || boot.Protocol != protocolVersion {
			return errors.New("unsupported live protocol")
		}
	}
	if err = validate(boot.Request); err != nil {
		return err
	}
	req := boot.Request
	attempts := 5
	if env := os.Getenv("UNREAL_HARNESS_LLM_MAX_ATTEMPTS"); env != "" {
		if _, err = fmt.Sscan(env, &attempts); err != nil {
			return errors.New("invalid max attempts")
		}
	}
	if req.Attempts != nil {
		attempts = *req.Attempts
	}
	if attempts < 1 {
		return errors.New("max attempts must be positive")
	}
	model := req.Model
	if model == "" {
		model = os.Getenv("UNREAL_HARNESS_LLM_MODEL")
	}
	if model == "" {
		return errors.New("model must be selected")
	}
	adapter, err := newClient(os.Getenv("UNREAL_HARNESS_LLM_PROVIDER"), os.Getenv("UNREAL_HARNESS_LLM_BASE_URL"), os.Getenv("UNREAL_HARNESS_LLM_API_KEY"), attempts)
	if err != nil {
		return err
	}
	defer adapter.Close()
	var modelAdapter llm.Adapter = adapter
	if req.Context != nil {
		if req.Context.Window < 0 || req.Context.Reserve < 0 || req.Context.Keep < 0 {
			return errors.New("invalid context configuration")
		}
		maintained := newCompactingAdapter(adapter, *req.Context, filepath.Join(*dir, "context", req.SessionID, "checkpoint.json"), out.emit)
		if req.Compact {
			maintained.forceCompact()
		}
		modelAdapter = maintained
	}
	store, err := localfile.New(*dir)
	if err != nil {
		return err
	}
	sid := session.ID(req.SessionID)
	restored, err := store.Resume(parent, sid)
	if errors.Is(err, fs.ErrNotExist) {
		snapshot, e := store.Create(parent, sid)
		if e != nil {
			return e
		}
		restored = sessionstore.ResumeState{Snapshot: snapshot}
		err = nil
	}
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	if *parentPID > 1 {
		go func() {
			ticker := time.NewTicker(time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
					if errors.Is(syscall.Kill(*parentPID, 0), syscall.ESRCH) {
						cancel()
						return
					}
				}
			}
		}()
	}
	operationDir := filepath.Join(*dir, "operations", req.SessionID)
	if err = os.MkdirAll(operationDir, 0700); err != nil {
		return err
	}
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/sh"
	}
	catalog := discoverSkills(abs)
	for _, warning := range catalog.Diagnostics {
		if !strings.Contains(warning, ": shadowed ") {
			fmt.Fprintln(os.Stderr, "skill warning: "+warning)
		}
	}
	skills := []tool.Skill{}
	automatic := []tool.Skill{}
	catalogBytes := 0
	omitted := 0
	access := &skillAccess{allowed: map[string]bool{}}
	for _, entry := range catalog.Skills {
		if !entry.Auto && !entry.User {
			continue
		}
		skills = append(skills, entry.native())
		if entry.Auto {
			visible := entry.native()
			description := []rune(visible.Description)
			if len(description) > 240 {
				visible.Description = string(description[:239]) + "…"
			}
			cost := len(visible.Name) + len(visible.Description) + len(visible.Path) + 128
			if catalogBytes+cost <= 32768 {
				automatic = append(automatic, visible)
				catalogBytes += cost
			} else {
				omitted++
			}
			access.allowed[entry.Name] = true
		}
	}
	if omitted > 0 {
		fmt.Fprintf(os.Stderr, "skill warning: %d automatic skills omitted from the 32 KiB catalog; /skills and explicit $name selection remain available\n", omitted)
	}
	names := []string{tool.BashName, tool.ViewImageName}
	if len(skills) > 0 {
		names = append(names, tool.SkillUseName)
	}
	enabled := []string{}
	for _, name := range names {
		blocked := false
		for _, skip := range req.Disallowed {
			if skip == name {
				blocked = true
			}
		}
		if !blocked {
			enabled = append(enabled, name)
		}
	}
	registry := skillRegistry{Registry: tool.NewRegistry(tool.StaticTranslators{Bash: bash.New(bash.Config{Shell: shell, Directory: abs, BaseDirectory: operationDir}), ViewImage: viewimage.New(viewimage.Config{Directory: abs})}, enabled...), access: access}
	if _, on := registry.Resolve(tool.SkillUseName); on {
		for _, skill := range skills {
			if _, err = registry.RegisterSkill(skill); err != nil {
				return err
			}
		}
	}
	if _, enabled := registry.Resolve(tool.SkillUseName); !enabled {
		automatic = nil
		catalog.Skills = nil
	}
	builder := skillBuilder{Builder: contextbuilder.NewBuilder(automatic...), entries: catalog.Skills, access: access}
	builder.SetModel(llm.Model{ID: model, ReasoningEffort: llm.ReasoningEffort(req.Thinking)})
	system := defaultSystemPrompt
	if req.SystemPrompt != nil {
		system = *req.SystemPrompt
	}
	builder.SetSystemPrompt(system)
	for _, definition := range registry.StaticDefinitions() {
		builder.AddTool(definition.Tool)
	}
	if req.CompactOnly {
		if len(restored.Operations) > 0 {
			return errors.New("unfinished operations; resume before compacting")
		}
		maintained := modelAdapter.(*compactingAdapter)
		return compactSession(ctx, store, sid, builder, registry, llm.Model{ID: model, ReasoningEffort: llm.ReasoningEffort(req.Thinking)}, maintained)
	}
	inputs, err := inbox.New(ctx, restored.ExternalInputIDs)
	if err != nil {
		return err
	}
	// Only the coordinator persists canonical inputs. Observer receipts are durable.
	accepted := map[inbox.ID]bool{}
	for _, id := range restored.ExternalInputIDs {
		accepted[id] = true
	}
	observed := store.AddObserver(func(id session.ID, item sessionstore.Item) {
		if id != sid {
			return
		}
		out.mu.Lock()
		defer out.mu.Unlock()
		out.write(item)
		if item.Kind == sessionstore.ItemInput {
			if input, ok := item.Data.(inbox.Input); ok && input.Kind == inbox.InputExternal {
				accepted[input.ID] = true
				out.write(map[string]any{"type": "live.accepted", "id": string(input.ID)})
			}
		}
	})
	defer store.RemoveObserver(observed)
	submit := func(id, text string) error {
		if err := validateID(id); err != nil {
			return err
		}
		if text == "" || len(text) > maxTextBytes {
			return errors.New("invalid input size")
		}
		out.mu.Lock()
		seen := accepted[inbox.ID(id)]
		out.mu.Unlock()
		if seen {
			out.emit(map[string]any{"type": "live.accepted", "id": id, "duplicate": true})
			return nil
		}
		payload, e := json.Marshal(text)
		if e != nil {
			return e
		}
		return inputs.Submit(ctx, inbox.Input{ID: inbox.ID(id), Kind: inbox.InputExternal, Payload: payload})
	}
	control := func(mode inbox.ControlMode, thinking string) error {
		msg := inbox.ControlMessage{Mode: mode}
		if mode == inbox.UpdateSettings {
			if !llm.ReasoningEffort(thinking).Valid() {
				return errors.New("invalid thinking level")
			}
			msg.Parameters = inbox.Settings{ReasoningEffort: llm.ReasoningEffort(thinking)}
		}
		payload, e := json.Marshal(msg)
		if e != nil {
			return e
		}
		return inputs.Submit(ctx, inbox.Input{ID: inbox.ID(uuid.New().String()), Kind: inbox.InputControl, Payload: payload})
	}
	if err = control(inbox.UpdateSettings, req.Thinking); err != nil {
		return err
	}
	for _, msg := range req.Messages {
		if err = submit(msg.ID, msg.Content); err != nil {
			return err
		}
	}
	// One process per work interval, not per message: supplements can join until idle.
	if err = control(inbox.StopWhenIdle, ""); err != nil {
		return err
	}
	out.emit(map[string]any{"type": "live.ready", "protocol": protocolVersion, "session_id": req.SessionID})
	go func() {
		for scan.Scan() {
			f, e := decode(scan.Bytes())
			if e != nil {
				out.emit(map[string]any{"type": "live.rejected", "message": "invalid command JSON"})
				continue
			}
			switch f.Type {
			case "input":
				e = submit(f.ID, f.Text)
			case "thinking":
				e = control(inbox.UpdateSettings, f.Thinking)
			case "stop":
				if f.Mode == "hard" {
					e = control(inbox.StopHard, "")
				} else if f.Mode == "when_idle" {
					e = control(inbox.StopWhenIdle, "")
				} else {
					e = errors.New("invalid stop mode")
				}
			default:
				e = errors.New("unknown live command")
			}
			if e != nil {
				out.emit(map[string]any{"type": "live.rejected", "id": f.ID, "message": e.Error()})
			}
		}
		if scan.Err() != nil {
			out.emit(map[string]any{"type": "live.rejected", "message": "input frame exceeds limit or stream failed"})
		}
	}()
	operations := operation.NewLocalOperationManager(ctx)
	current := coordinator.New(coordinator.Dependencies{ToolHeartbeatInterval: 10 * time.Minute, SessionID: sid, Inbox: inputs, Restored: restored, Sessions: store, ContextBuilder: builder, LLM: modelAdapter, Tools: registry, Operations: operations})
	err = current.Run(ctx)
	cancel()
	// Updates closes only after native primitives were canceled and drained.
	// Do not os.Exit while a tool process is still being cleaned up.
	for range operations.Updates() {
	}
	if writeErr := out.result(); writeErr != nil {
		return writeErr
	}
	if err != nil {
		return err
	}
	out.emit(map[string]any{"type": "live.finished", "session_id": req.SessionID})
	return nil
}
