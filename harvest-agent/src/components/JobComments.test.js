import { pickWorkspaceComment } from "./JobComments";

const call = (remarks, at) => ({ remarks, at });
const comment = (body, created_at, updated_at) => ({ body, created_at, updated_at, author_name: "Alice" });

test("newest of call remarks and job comment wins", () => {
  expect(pickWorkspaceComment({
    call: call("Called, no answer", "2026-11-20T10:00:00Z"),
    comment: comment("Emailed HR", "2026-11-21T10:00:00Z"),
  })).toMatchObject({ text: "Emailed HR", source: "comment", by: "Alice" });
  expect(pickWorkspaceComment({
    call: call("Spoke to HR", "2026-11-22T10:00:00Z"),
    comment: comment("Emailed HR", "2026-11-21T10:00:00Z"),
  })).toMatchObject({ text: "Spoke to HR", source: "call" });
});

test("an edited comment counts by its update time", () => {
  expect(pickWorkspaceComment({
    call: call("Spoke to HR", "2026-11-22T10:00:00Z"),
    comment: comment("Updated note", "2026-11-21T10:00:00Z", "2026-11-23T10:00:00Z"),
  }).source).toBe("comment");
});

test("falls back: one present → that one; neither → assignment note; nothing → null", () => {
  expect(pickWorkspaceComment({ call: call("only call", "2026-11-20T10:00:00Z") }).source).toBe("call");
  expect(pickWorkspaceComment({ call: call("", "2026-11-20T10:00:00Z"), comment: comment("only comment", "2026-11-20T10:00:00Z") }).source).toBe("comment");
  expect(pickWorkspaceComment({ call: null, comment: null, note: "Follow up Monday" }))
    .toEqual({ text: "Follow up Monday", source: "note" });
  expect(pickWorkspaceComment({})).toBeNull();
});
