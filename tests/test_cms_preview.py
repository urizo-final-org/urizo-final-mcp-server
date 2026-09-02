from __future__ import annotations

import unittest

from axms_mcp_server.cms.preview import (
    NaturalCmsToolError,
    apply_preview,
    create_preview,
    discard_preview,
    resolve_target,
    revalidate_preview,
    validate_command,
)


RESOURCE = {"type": "CONTENT", "id": "42"}
CURRENT = {"id": 42, "title": "Before", "body": "Old body"}
COMMAND = {
    "operation": "UPDATE",
    "fields": {"title": "After", "body": "New body"},
}
CREATE = {
    "operation": "CREATE",
    "fields": {"title": "New title", "body": "New body"},
}
DELETE = {"operation": "DELETE", "fields": {}}


class NaturalCmsPreviewTest(unittest.TestCase):
    def test_preview_revalidation_and_apply_are_deterministic(self) -> None:
        resolved = resolve_target(RESOURCE, CURRENT)
        validated = validate_command(RESOURCE, COMMAND, CURRENT)
        preview = create_preview(RESOURCE, COMMAND, CURRENT)

        self.assertTrue(resolved["resolved"])
        self.assertTrue(validated["valid"])
        self.assertEqual("After", preview["after"]["title"])
        self.assertTrue(
            revalidate_preview(
                preview["previewId"],
                preview["previewHash"],
                RESOURCE,
                COMMAND,
                CURRENT,
            )["valid"]
        )
        apply_ready = apply_preview(
            preview["previewId"],
            preview["previewHash"],
            RESOURCE,
            COMMAND,
            CURRENT,
        )
        self.assertTrue(apply_ready["applyReady"])
        self.assertEqual(COMMAND, apply_ready["command"])

    def test_changed_resource_is_stale_and_discard_is_side_effect_free(self) -> None:
        preview = create_preview(RESOURCE, COMMAND, CURRENT)
        changed = {**CURRENT, "body": "Changed elsewhere"}

        self.assertFalse(
            revalidate_preview(
                preview["previewId"],
                preview["previewHash"],
                RESOURCE,
                COMMAND,
                changed,
            )["valid"]
        )
        with self.assertRaisesRegex(NaturalCmsToolError, "no longer matches"):
            apply_preview(
                preview["previewId"],
                preview["previewHash"],
                RESOURCE,
                COMMAND,
                changed,
            )
        self.assertTrue(
            discard_preview(preview["previewId"], preview["previewHash"])[
                "discarded"
            ]
        )

    def test_rejects_coding_fields_and_unknown_command_shape(self) -> None:
        with self.assertRaises(NaturalCmsToolError):
            resolve_target({**RESOURCE, "candidateSha": "not-cms"}, CURRENT)
        with self.assertRaises(NaturalCmsToolError):
            validate_command(
                RESOURCE,
                {**COMMAND, "workspaceId": "not-cms"},
                CURRENT,
            )

    def test_delete_carries_no_fields_and_leaves_nothing_behind(self) -> None:
        preview = create_preview(RESOURCE, DELETE, CURRENT)

        self.assertEqual({}, preview["after"])
        self.assertEqual(CURRENT, preview["before"])
        self.assertNotEqual(
            create_preview(RESOURCE, COMMAND, CURRENT)["previewHash"],
            preview["previewHash"],
        )
        self.assertTrue(
            apply_preview(
                preview["previewId"], preview["previewHash"], RESOURCE, DELETE, CURRENT
            )["applyReady"]
        )
        with self.assertRaises(NaturalCmsToolError):
            validate_command(RESOURCE, {**DELETE, "fields": {"title": "x"}}, CURRENT)

    def test_create_needs_fields_and_starts_from_the_draft_state(self) -> None:
        draft = {"id": "draft-1"}
        resource = {"type": "CONTENT", "id": "draft-1"}
        preview = create_preview(resource, CREATE, draft)

        self.assertEqual(draft, preview["before"])
        self.assertEqual("New title", preview["after"]["title"])
        with self.assertRaises(NaturalCmsToolError):
            validate_command(resource, {**CREATE, "fields": {}}, draft)

    def test_accepts_number_boolean_and_null_fields_only(self) -> None:
        accepted = {"position": 3, "active": True, "contentId": None, "title": "Kept"}
        validated = validate_command(
            RESOURCE, {"operation": "UPDATE", "fields": accepted}, CURRENT
        )

        self.assertEqual(accepted, validated["command"]["fields"])
        for rejected in ({"ratio": 1.5}, {"parentId": 2**53}, {"tags": ["a"]}):
            with self.assertRaises(NaturalCmsToolError):
                validate_command(
                    RESOURCE, {"operation": "UPDATE", "fields": rejected}, CURRENT
                )

    def test_rejects_operations_outside_the_approved_set(self) -> None:
        for operation in ("REORDER", "PATCH", "update", ""):
            with self.assertRaises(NaturalCmsToolError):
                validate_command(
                    RESOURCE, {"operation": operation, "fields": {"title": "x"}}, CURRENT
                )


if __name__ == "__main__":
    unittest.main()
