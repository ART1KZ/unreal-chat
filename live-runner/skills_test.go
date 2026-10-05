package main

import (
	"encoding/json/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/unreallabsai/unreal-agent/harness/contextbuilder"
	"github.com/unreallabsai/unreal-agent/harness/inbox"
	"github.com/unreallabsai/unreal-agent/harness/llm"
	"github.com/unreallabsai/unreal-agent/harness/tool"
)

func fixtureSkill(t *testing.T, root, folder, metadata string) string {
	t.Helper()
	path := filepath.Join(root, folder, "SKILL.md")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(metadata+"\nBODY_NOT_IN_CATALOG\n"), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestDiscoveryYAMLPrecedenceOriginalPathsAndCycles(t *testing.T) {
	root := t.TempDir()
	project, user := filepath.Join(root, "project"), filepath.Join(root, "user")
	chosen := fixtureSkill(t, project, "review", "---\nname: review\ndescription: >-\n  Review code\n  carefully.\n---")
	fixtureSkill(t, user, "review", "---\nname: review\ndescription: Global review\n---")
	fixtureSkill(t, user, "collection/quoted", "---\nname: quoted\ndescription: 'Handle: quoted descriptions'\n---")
	fixtureSkill(t, user, "broken", "---\nname: broken\ndescription: [not a scalar]\n---")
	if err := os.Symlink(user, filepath.Join(user, "cycle")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Dir(chosen), filepath.Join(user, "alias")); err != nil {
		t.Fatal(err)
	}
	catalog := discoverSkillRoots([]skillRoot{{project, "project"}, {user, "user"}})
	if len(catalog.Skills) != 2 {
		t.Fatalf("catalog: %+v", catalog)
	}
	for _, entry := range catalog.Skills {
		if entry.Name == "review" && (entry.Path != chosen || entry.Description != "Review code carefully.") {
			t.Fatalf("wrong winner: %+v", entry)
		}
	}
	encoded, _ := json.Marshal(catalog)
	if strings.Contains(string(encoded), "BODY_NOT_IN_CATALOG") {
		t.Fatal("skill body leaked into catalog")
	}
	if len(catalog.Diagnostics) != 2 {
		t.Fatalf("expected malformed and shadow diagnostics, got %+v", catalog.Diagnostics)
	}
}

func TestProjectBoundaryAndExtraPriority(t *testing.T) {
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	nested := filepath.Join(repo, "a", "b")
	if err := os.MkdirAll(nested, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, ".git"), []byte("gitdir: /example"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("UACHAT_SKILL_DIRS", filepath.Join(root, "extra"))
	roots := skillRoots(nested, filepath.Join(root, "home"), filepath.Join(root, "win"))
	if roots[0].path != filepath.Join(nested, ".harness", "skills") {
		t.Fatal(roots)
	}
	for _, item := range roots {
		if item.source == "project" && !strings.HasPrefix(item.path, repo+string(os.PathSeparator)) {
			t.Fatalf("escaped repo: %+v", item)
		}
	}
	if roots[15].source != "extra" {
		t.Fatal("extra roots must follow project scope")
	}
	outside := skillRoots(root, "", "")
	for _, item := range outside {
		if item.source == "project" && filepath.Dir(filepath.Dir(item.path)) != root {
			t.Fatalf("unbounded discovery: %+v", item)
		}
	}
}

func TestInvocationPolicyAndExactMention(t *testing.T) {
	root := t.TempDir()
	path := fixtureSkill(t, root, "manual", "---\nname: manual\ndescription: Manual only\ndisable-model-invocation: true\n---")
	hidden := fixtureSkill(t, root, "hidden", "---\nname: hidden\ndescription: Background\nuser-invocable: false\n---")
	codex := fixtureSkill(t, root, "codex", "---\nname: codex\ndescription: Codex policy\n---")
	policy := filepath.Join(filepath.Dir(codex), "agents", "openai.yaml")
	os.MkdirAll(filepath.Dir(policy), 0700)
	os.WriteFile(policy, []byte("policy:\n  allow_implicit_invocation: false\n"), 0600)
	catalog := discoverSkillRoots([]skillRoot{{root, "user"}})
	entries := map[string]skillEntry{}
	for _, entry := range catalog.Skills {
		entries[entry.Name] = entry
	}
	if entries["manual"].Auto || entries["codex"].Auto || entries["hidden"].User {
		t.Fatal(entries)
	}
	if entries["manual"].Path != path || entries["hidden"].Path != hidden {
		t.Fatal(entries)
	}
	if len(explicitSkills("$manual-other $hidden", catalog.Skills)) != 0 {
		t.Fatal("partial or hidden mention accepted")
	}
	if len(explicitSkills("Please $manual, do this", catalog.Skills)) != 1 {
		t.Fatal("exact mention missing")
	}
}

func TestManualSkillGateAndModelOnlyEnrichment(t *testing.T) {
	access := &skillAccess{allowed: map[string]bool{}}
	registry := skillRegistry{Registry: tool.NewRegistry(tool.StaticTranslators{}, tool.SkillUseName), access: access}
	translator, _ := registry.Resolve(tool.SkillUseName)
	call := llm.ToolCall{Name: tool.SkillUseName, Arguments: `{"name":"manual"}`}
	if !strings.Contains(translator.Translate(nil, call).Error, "explicitly") {
		t.Fatal("manual-only gate bypassed")
	}
	builder := skillBuilder{Builder: contextbuilder.NewBuilder(), entries: []skillEntry{{Name: "manual", Path: "/skill/manual/SKILL.md", User: true}}, access: access}
	payload, _ := json.Marshal("Use $manual")
	input := inbox.Input{Kind: inbox.InputExternal, Payload: payload}
	original := string(input.Payload)
	if err := builder.AddExternalInput(input); err != nil {
		t.Fatal(err)
	}
	if !access.allowed["manual"] {
		t.Fatal("explicit selection did not grant skill")
	}
	if string(input.Payload) != original {
		t.Fatal("canonical input mutated")
	}
	// No registered file, so a successful permission check proceeds to the
	// native registry and returns its own registration error.
	if !strings.Contains(translator.Translate(nil, call).Error, "not registered") {
		t.Fatal("explicit selection still blocked")
	}
}

func TestWindowsEmptyForwardedOverrideFallsBackToProfile(t *testing.T) {
	directory := t.TempDir()
	for name, result := range map[string]string{"powershell.exe": "C:/Users/probe", "wslpath": "/mnt/c/Users/probe"} {
		if err := os.WriteFile(filepath.Join(directory, name), []byte("#!/bin/sh\nprintf '"+result+"\\n'\n"), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", directory)
	t.Setenv("WSL_DISTRO_NAME", "Ubuntu")
	t.Setenv("UACHAT_WINDOWS_HOME", "")
	if result := windowsSkillHome(); result != "/mnt/c/Users/probe" {
		t.Fatalf("empty forwarded override suppressed discovery: %q", result)
	}
	t.Setenv("UACHAT_WINDOWS_HOME", "off")
	if result := windowsSkillHome(); result != "" {
		t.Fatal("off setting ignored")
	}
	t.Setenv("UACHAT_WINDOWS_HOME", "/mnt/c/Users/explicit")
	if result := windowsSkillHome(); result != "/mnt/c/Users/explicit" {
		t.Fatal("explicit profile ignored")
	}
}
