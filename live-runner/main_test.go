package main

import (
	"encoding/json/v2"
	"testing"
	"uuid"
)

func TestValidation(t *testing.T) {
	prompt := "hello"
	for _, id := range []string{"", "../escape", "bad/id", "bad\\id", "bad\x00id"} {
		req := &request{Prompt: &prompt, SessionID: id}
		if validate(req) == nil {
			t.Fatalf("accepted bad session %q", id)
		}
	}
	req := &request{Prompt: &prompt, SessionID: "session"}
	if err := validate(req); err != nil {
		t.Fatal(err)
	}
	if _, err := uuid.Parse(req.Messages[0].ID); err != nil {
		t.Fatal("input does not have a stable UUID")
	}
}
func TestDecodeRejectsUnknownFields(t *testing.T) {
	if _, err := decode([]byte(`{"type":"input","id":"known","text":"hello","unexpected":true}`)); err == nil {
		t.Fatal("unknown field accepted")
	}
}
func TestWrongRoleAndID(t *testing.T) {
	req := &request{SessionID: "session", Messages: []message{{Role: "system", Content: "hello", ID: uuid.New().String()}}}
	if validate(req) == nil {
		t.Fatal("system input accepted")
	}
	req.Messages[0].Role = "user"
	req.Messages[0].ID = "bad"
	if validate(req) == nil {
		t.Fatal("invalid UUID accepted")
	}
}
func TestRoundTripRequest(t *testing.T) {
	encoded := []byte(`{"type":"start","protocol":1,"request":{"session_id":"test","prompt":"hello","model":"mock","thinking_level":"high"}}`)
	f, err := decode(encoded)
	if err != nil {
		t.Fatal(err)
	}
	if err = validate(f.Request); err != nil {
		t.Fatal(err)
	}
	if _, err = json.Marshal(f); err != nil {
		t.Fatal(err)
	}
}
