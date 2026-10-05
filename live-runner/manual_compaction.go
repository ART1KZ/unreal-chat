package main

import (
	"context"
	"errors"
	"github.com/unreallabsai/unreal-agent/harness/contextbuilder"
	"github.com/unreallabsai/unreal-agent/harness/inbox"
	"github.com/unreallabsai/unreal-agent/harness/llm"
	"github.com/unreallabsai/unreal-agent/harness/operation"
	"github.com/unreallabsai/unreal-agent/harness/session"
	"github.com/unreallabsai/unreal-agent/harness/sessionstore"
)

// Read-only reconstruction through public SDK builder and tool translators.
// No coordinator run, inbox input, tool dispatch or canonical journal writes.
func compactSession(ctx context.Context, store sessionstore.Store, sid session.ID, builder contextbuilder.Builder, registry skillRegistry, model llm.Model, adapter *compactingAdapter) error {
	after := sessionstore.BeforeFirst
	calls := map[string]llm.ToolCall{}
	operations := map[operation.ID]operation.Operation{}
	callOperations := map[string]map[operation.ID]bool{}
	turnType := session.TurnRegular
	for {
		page, err := store.Items(ctx, sid, after, 256)
		if err != nil {
			return err
		}
		for _, item := range page.Items {
			switch value := item.Data.(type) {
			case inbox.Input:
				if value.Kind == inbox.InputExternal {
					if err := builder.AddExternalInput(value); err != nil {
						return err
					}
				}
				if value.Kind == inbox.InputControl {
					control, err := value.DecodeControlMessage()
					if err != nil {
						return err
					}
					builder.AddControlMessage(control)
				}
			case session.Turn:
				turnType = value.Type
				builder.Commit()
			case sessionstore.ModelResponse:
				if turnType == session.TurnCompaction {
					continue
				}
				builder.AddModelResponse(value.Response)
				for _, item := range value.Response.Output {
					if call, ok := item.Data.(llm.ToolCall); ok {
						calls[call.CallID] = call
					}
				}
			case sessionstore.ToolCallStatus:
				call, ok := calls[value.CallID]
				if !ok {
					continue
				}
				for _, op := range value.Operations {
					operations[op.ID] = op
				}
				if callOperations[value.CallID] == nil {
					callOperations[value.CallID] = map[operation.ID]bool{}
				}
				for _, id := range value.Status.WaitingFor {
					callOperations[value.CallID][id] = true
				}
				var waiting []operation.Operation
				available := true
				for _, id := range value.Status.WaitingFor {
					op, exists := operations[id]
					if !exists {
						available = false
						break
					}
					waiting = append(waiting, op)
				}
				if !available {
					continue
				}
				terminal := true
				for id := range callOperations[value.CallID] {
					op, exists := operations[id]
					if !exists || (op.Status != operation.StatusCompleted && op.Status != operation.StatusFailed && op.Status != operation.StatusCanceled) {
						terminal = false
					}
				}
				translator, exists := registry.Resolve(call.Name)
				if !exists {
					return errors.New("recorded tool translator unavailable for manual compaction")
				}
				result, err := translator.TranslateResult(value.CallID, value.Status, waiting)
				if err != nil {
					return err
				}
				builder.AddToolResult(value.CallID, result.Output, !terminal)
				if terminal {
					delete(calls, value.CallID)
					delete(callOperations, value.CallID)
				}
			case sessionstore.Fork:
				return errors.New("manual compaction of fork journals is not supported; use automatic compaction during continuation")
			}
		}
		if !page.More {
			break
		}
		if page.NextAfter <= after {
			return errors.New("history pagination did not advance")
		}
		after = page.NextAfter
	}
	if len(calls) > 0 {
		return errors.New("unfinished tool calls; resume the session before compacting")
	}
	builder.SetModel(model)
	builder.Commit()
	built, err := builder.Build()
	if err != nil {
		return err
	}
	_, err = adapter.compact(ctx, built.Request, true)
	return err
}
