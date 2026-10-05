package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json/v2"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/unreallabsai/unreal-agent/harness/contextbuilder"
	"github.com/unreallabsai/unreal-agent/harness/inbox"
	"github.com/unreallabsai/unreal-agent/harness/llm"
	"github.com/unreallabsai/unreal-agent/harness/tool"
	"go.yaml.in/yaml/v3"
)

// The client and runner use this same catalog, retaining original file paths.
type skillEntry struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Path        string `json:"path"`
	Source      string `json:"source"`
	Auto        bool   `json:"auto"`
	User        bool   `json:"user"`
}
type skillCatalog struct {
	Skills      []skillEntry `json:"skills"`
	Diagnostics []string     `json:"diagnostics"`
	Roots       []string     `json:"roots"`
}
type skillRoot struct{ path, source string }

func windowsSkillHome() string {
	if value, ok := os.LookupEnv("UACHAT_WINDOWS_HOME"); ok && strings.TrimSpace(value) != "" {
		if value == "off" {
			return ""
		}
		if filepath.IsAbs(value) {
			return value
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		converted, err := exec.CommandContext(ctx, "wslpath", "-u", value).Output()
		if err == nil {
			return strings.TrimSpace(string(converted))
		}
		return value
	}
	if os.Getenv("WSL_DISTRO_NAME") == "" {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	raw, err := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "[Environment]::GetFolderPath('UserProfile')").Output()
	if err != nil {
		return ""
	}
	converted, err := exec.CommandContext(ctx, "wslpath", "-u", strings.TrimSpace(string(raw))).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(converted))
}

func skillRoots(workspace, home, windowsHome string) []skillRoot {
	relative := []string{".harness/skills", ".agents/skills", ".claude/skills", ".opencode/skills", ".codex/skills"}
	roots := []skillRoot{}
	// Stop at the nearest git worktree (a .git file also counts). Outside a
	// repository, only the explicitly selected workspace is project scope.
	boundary := workspace
	for current := workspace; ; current = filepath.Dir(current) {
		if _, err := os.Stat(filepath.Join(current, ".git")); err == nil {
			boundary = current
			break
		}
		if filepath.Dir(current) == current {
			break
		}
	}
	for current := workspace; ; current = filepath.Dir(current) {
		for _, dir := range relative {
			roots = append(roots, skillRoot{filepath.Join(current, dir), "project"})
		}
		if current == boundary {
			break
		}
	}
	for _, path := range filepath.SplitList(os.Getenv("UACHAT_SKILL_DIRS")) {
		if path != "" {
			roots = append(roots, skillRoot{path, "extra"})
		}
	}
	personal := []string{".config/uachat/skills", ".agents/skills", ".harness/skills", ".claude/skills", ".config/opencode/skills", ".opencode/skills", ".codex/skills"}
	for _, host := range []struct{ home, source string }{{home, "user"}, {windowsHome, "windows-user"}} {
		if host.home == "" {
			continue
		}
		for _, dir := range personal {
			roots = append(roots, skillRoot{filepath.Join(host.home, dir), host.source})
		}
	}
	return roots
}

var skillName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$`)

func readSkill(path, source string) (skillEntry, error) {
	file, err := os.Open(path)
	if err != nil {
		return skillEntry{}, err
	}
	defer file.Close()
	// Only read metadata, never pull every skill body into model context.
	scanner := bufio.NewScanner(io.LimitReader(file, 65537))
	scanner.Buffer(make([]byte, 4096), 65536)
	if !scanner.Scan() || strings.TrimPrefix(strings.TrimSpace(scanner.Text()), "\ufeff") != "---" {
		return skillEntry{}, fmt.Errorf("missing YAML frontmatter")
	}
	var metadata bytes.Buffer
	closed := false
	for scanner.Scan() {
		if strings.TrimSpace(scanner.Text()) == "---" {
			closed = true
			break
		}
		metadata.WriteString(scanner.Text())
		metadata.WriteByte('\n')
	}
	if !closed {
		return skillEntry{}, fmt.Errorf("missing closing YAML delimiter or metadata exceeds 64 KiB")
	}
	var value struct {
		Name        string `yaml:"name"`
		Description string `yaml:"description"`
		DisableAuto bool   `yaml:"disable-model-invocation"`
		User        *bool  `yaml:"user-invocable"`
	}
	if err := yaml.Unmarshal(metadata.Bytes(), &value); err != nil {
		return skillEntry{}, fmt.Errorf("invalid YAML metadata")
	}
	name, description := strings.TrimSpace(value.Name), strings.TrimSpace(value.Description)
	if !skillName.MatchString(name) {
		return skillEntry{}, fmt.Errorf("name must be 1–64 letters, digits, hyphens, underscores, dots or colons")
	}
	if description == "" || len([]rune(description)) > 1024 {
		return skillEntry{}, fmt.Errorf("description must contain 1–1024 characters")
	}
	entry := skillEntry{Name: name, Description: description, Path: path, Source: source, Auto: !value.DisableAuto, User: true}
	if value.User != nil {
		entry.User = *value.User
	}
	// Codex's invocation policy also applies to skills shared with this client.
	policyPath := filepath.Join(filepath.Dir(path), "agents", "openai.yaml")
	policy, err := os.ReadFile(policyPath)
	if err == nil {
		var document struct {
			Policy struct {
				Implicit *bool `yaml:"allow_implicit_invocation"`
			} `yaml:"policy"`
		}
		if yaml.Unmarshal(policy, &document) != nil {
			return skillEntry{}, fmt.Errorf("invalid agents/openai.yaml")
		}
		if document.Policy.Implicit != nil && !*document.Policy.Implicit {
			entry.Auto = false
		}
	} else if !os.IsNotExist(err) {
		return skillEntry{}, fmt.Errorf("cannot read agents/openai.yaml")
	}
	return entry, nil
}

func discoverSkillRoots(roots []skillRoot) skillCatalog {
	catalog := skillCatalog{Skills: []skillEntry{}, Diagnostics: []string{}, Roots: []string{}}
	seenDirs, seenFiles, names := map[string]bool{}, map[string]bool{}, map[string]string{}
	visited := 0
	exhausted := false
	var visit func(string, string, int)
	visit = func(directory, source string, depth int) {
		if exhausted {
			return
		}
		if depth > 8 {
			catalog.Diagnostics = append(catalog.Diagnostics, directory+": skill nesting exceeds 8 levels")
			return
		}
		canonical, err := filepath.EvalSymlinks(directory)
		if os.IsNotExist(err) {
			return
		}
		if err != nil {
			catalog.Diagnostics = append(catalog.Diagnostics, directory+": cannot resolve directory")
			return
		}
		if seenDirs[canonical] {
			return
		}
		seenDirs[canonical] = true
		visited++
		if visited > 2000 {
			exhausted = true
			catalog.Diagnostics = append(catalog.Diagnostics, "skill discovery stopped after 2000 directories")
			return
		}
		path := filepath.Join(canonical, "SKILL.md")
		if _, err := os.Stat(path); err == nil {
			original, err := filepath.EvalSymlinks(path)
			if err != nil {
				catalog.Diagnostics = append(catalog.Diagnostics, path+": cannot resolve skill file")
				return
			}
			if seenFiles[original] {
				return
			}
			seenFiles[original] = true
			entry, err := readSkill(original, source)
			if err != nil {
				catalog.Diagnostics = append(catalog.Diagnostics, path+": "+err.Error())
				return
			}
			if winner, ok := names[entry.Name]; ok {
				catalog.Diagnostics = append(catalog.Diagnostics, fmt.Sprintf("%s: shadowed %q; using %s", path, entry.Name, winner))
				return
			}
			names[entry.Name] = entry.Path
			catalog.Skills = append(catalog.Skills, entry)
			return // scripts/references/assets inside a skill are resources.
		} else if !os.IsNotExist(err) {
			catalog.Diagnostics = append(catalog.Diagnostics, path+": cannot inspect skill file")
			return
		}
		children, err := os.ReadDir(canonical)
		if err != nil {
			catalog.Diagnostics = append(catalog.Diagnostics, directory+": cannot scan directory")
			return
		}
		for _, child := range children {
			if child.Name() == ".git" || child.Name() == "node_modules" {
				continue
			}
			next := filepath.Join(canonical, child.Name())
			info, err := os.Stat(next)
			if err == nil && info.IsDir() {
				visit(next, source, depth+1)
			}
		}
	}
	for _, root := range roots {
		absolute, err := filepath.Abs(root.path)
		if err != nil {
			catalog.Diagnostics = append(catalog.Diagnostics, root.path+": invalid root")
			continue
		}
		catalog.Roots = append(catalog.Roots, absolute)
		visit(absolute, root.source, 0)
	}
	sort.Slice(catalog.Skills, func(i, j int) bool { return catalog.Skills[i].Name < catalog.Skills[j].Name })
	return catalog
}

func discoverSkills(workspace string) skillCatalog {
	home, _ := os.UserHomeDir()
	return discoverSkillRoots(skillRoots(workspace, home, windowsSkillHome()))
}

func (entry skillEntry) native() tool.Skill {
	return tool.Skill{Name: entry.Name, Description: entry.Description, Path: entry.Path}
}

// A manual-only skill is registered for this work interval only after an
// explicit $name or /skill name request. Hiding metadata alone is insufficient.
func explicitSkills(text string, entries []skillEntry) []skillEntry {
	chosen := []skillEntry{}
	for _, entry := range entries {
		if !entry.User {
			continue
		}
		pattern := regexp.MustCompile(`(^|\s)\$` + regexp.QuoteMeta(entry.Name) + `($|[^A-Za-z0-9_.:-])`)
		if pattern.MatchString(text) {
			chosen = append(chosen, entry)
		}
	}
	return chosen
}

func prepareSkillMessages(messages []message, entries []skillEntry) ([]message, map[string]bool) {
	allowed := map[string]bool{}
	for index := range messages {
		chosen := explicitSkills(messages[index].Content, entries)
		if len(chosen) == 0 {
			continue
		}
		selection := []map[string]string{}
		for _, entry := range chosen {
			allowed[entry.Name] = true
			selection = append(selection, map[string]string{"name": entry.Name, "path": entry.Path})
		}
		data, _ := json.Marshal(selection)
		messages[index].Content += "\n\nExplicit skill selection (user request): load these skills with SkillUse before proceeding. Resolve relative resources against each skill's original directory.\n" + string(data)
	}
	return messages, allowed
}

type skillAccess struct {
	mu      sync.RWMutex
	allowed map[string]bool
}
type skillRegistry struct {
	tool.Registry
	access *skillAccess
}
type skillTranslator struct {
	tool.Translator
	access *skillAccess
}

func (registry skillRegistry) Resolve(name string) (tool.Translator, bool) {
	translator, ok := registry.Registry.Resolve(name)
	if ok && name == tool.SkillUseName {
		return skillTranslator{translator, registry.access}, true
	}
	return translator, ok
}
func (translator skillTranslator) Translate(ctx tool.Context, call llm.ToolCall) tool.CallStatus {
	var arguments struct {
		Name string `json:"name"`
	}
	if json.Unmarshal([]byte(call.Arguments), &arguments) == nil {
		translator.access.mu.RLock()
		allowed := translator.access.allowed[arguments.Name]
		translator.access.mu.RUnlock()
		if !allowed {
			return tool.CallStatus{Error: "Skill is unavailable for automatic invocation. Ask the user to select it explicitly with $name or /skill name."}
		}
	}
	return translator.Translator.Translate(ctx, call)
}

type skillBuilder struct {
	contextbuilder.Builder
	entries []skillEntry
	access  *skillAccess
}

func (builder skillBuilder) AddExternalInput(input inbox.Input) error {
	var text string
	if err := json.Unmarshal(input.Payload, &text); err != nil {
		return err
	}
	messages, allowed := prepareSkillMessages([]message{{Content: text}}, builder.entries)
	builder.access.mu.Lock()
	for name := range allowed {
		builder.access.allowed[name] = true
	}
	builder.access.mu.Unlock()
	// Enrich only model context. Canonical user messages/receipts stay intact.
	payload, err := json.Marshal(messages[0].Content)
	if err != nil {
		return err
	}
	input.Payload = payload
	return builder.Builder.AddExternalInput(input)
}
