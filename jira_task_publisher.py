#!/usr/bin/env python3
"""
jira_task_publisher.py

Simple v1 CLI script to create Jira issues from a JSON file.

Features:
- Reads Jira credentials from environment variables:
  - JIRA_BASE_URL
  - JIRA_EMAIL
  - JIRA_API_TOKEN
- Reads local config JSON for project settings and epic mapping
- Reads input JSON from a file path passed on the command line
- Creates issues under a mapped epic
- Adds labels
- Formats acceptance criteria into the Jira description
- Logs successes and failures clearly
- Continues processing if one issue fails

Usage:
    python jira_task_publisher.py --config config.json --input tasks.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

# -----------------------------
# Data models
# -----------------------------


@dataclass
class JiraCredentials:
    base_url: str
    email: str
    api_token: str


@dataclass
class AppConfig:
    project_key: str
    issue_type: str
    epic_field_mode: str
    epic_link_field_id: Optional[str]
    epic_name_to_key: Dict[str, str]


# -----------------------------
# Utility / logging
# -----------------------------


def log_info(message: str) -> None:
    print(f"[INFO] {message}")


def log_success(message: str) -> None:
    print(f"[OK]   {message}")


def log_error(message: str) -> None:
    print(f"[ERR]  {message}", file=sys.stderr)


def truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# -----------------------------
# Validation and loading
# -----------------------------


def load_env_credentials() -> JiraCredentials:
    base_url = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    api_token = os.getenv("JIRA_API_TOKEN", "").strip()

    missing = []
    if not base_url:
        missing.append("JIRA_BASE_URL")
    if not email:
        missing.append("JIRA_EMAIL")
    if not api_token:
        missing.append("JIRA_API_TOKEN")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    return JiraCredentials(base_url=base_url, email=email, api_token=api_token)


def load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise ValueError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in file {path}: {exc}") from exc


def load_config(path: str) -> AppConfig:
    raw = load_json_file(path)

    required = ["project_key", "issue_type", "epic_field_mode", "epic_name_to_key"]
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"Config file is missing required keys: {', '.join(missing)}")

    epic_field_mode = raw["epic_field_mode"]
    if epic_field_mode not in {"parent", "custom_field"}:
        raise ValueError(
            "config.epic_field_mode must be either 'parent' or 'custom_field'"
        )

    epic_link_field_id = raw.get("epic_link_field_id")
    if epic_field_mode == "custom_field" and not epic_link_field_id:
        raise ValueError(
            "config.epic_link_field_id is required when epic_field_mode='custom_field'"
        )

    epic_name_to_key = raw["epic_name_to_key"]
    if not isinstance(epic_name_to_key, dict) or not epic_name_to_key:
        raise ValueError("config.epic_name_to_key must be a non-empty object")

    return AppConfig(
        project_key=str(raw["project_key"]).strip(),
        issue_type=str(raw["issue_type"]).strip(),
        epic_field_mode=epic_field_mode,
        epic_link_field_id=epic_link_field_id,
        epic_name_to_key=epic_name_to_key,
    )


def validate_input_payload(data: Any) -> Tuple[str, List[Dict[str, Any]]]:
    if not isinstance(data, dict):
        raise ValueError("Input JSON must be an object")

    if "epic_name" not in data:
        raise ValueError("Input JSON must include 'epic_name'")
    if "issues" not in data:
        raise ValueError("Input JSON must include 'issues'")

    epic_name = data["epic_name"]
    issues = data["issues"]

    if not isinstance(epic_name, str) or not epic_name.strip():
        raise ValueError("'epic_name' must be a non-empty string")

    if not isinstance(issues, list) or not issues:
        raise ValueError("'issues' must be a non-empty array")

    validated_issues: List[Dict[str, Any]] = []

    for index, issue in enumerate(issues, start=1):
        if not isinstance(issue, dict):
            raise ValueError(f"Issue #{index} must be an object")

        required = ["summary", "description", "acceptance_criteria", "labels"]
        missing = [key for key in required if key not in issue]
        if missing:
            raise ValueError(
                f"Issue #{index} is missing required keys: {', '.join(missing)}"
            )

        summary = issue["summary"]
        description = issue["description"]
        acceptance_criteria = issue["acceptance_criteria"]
        labels = issue["labels"]

        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"Issue #{index} 'summary' must be a non-empty string")

        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"Issue #{index} 'description' must be a non-empty string")

        if not isinstance(acceptance_criteria, list) or not acceptance_criteria:
            raise ValueError(
                f"Issue #{index} 'acceptance_criteria' must be a non-empty array"
            )

        for ac_index, item in enumerate(acceptance_criteria, start=1):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"Issue #{index} acceptance_criteria item #{ac_index} "
                    f"must be a non-empty string"
                )

        if not isinstance(labels, list):
            raise ValueError(f"Issue #{index} 'labels' must be an array")

        for label_index, label in enumerate(labels, start=1):
            if not isinstance(label, str) or not label.strip():
                raise ValueError(
                    f"Issue #{index} labels item #{label_index} must be a non-empty string"
                )

        validated_issues.append(
            {
                "summary": summary.strip(),
                "description": description.strip(),
                "acceptance_criteria": [item.strip() for item in acceptance_criteria],
                "labels": [label.strip() for label in labels],
            }
        )

    return epic_name.strip(), validated_issues


# -----------------------------
# Jira ADF helpers
# -----------------------------


def adf_text_paragraph(text: str) -> Dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
    }


def adf_heading(text: str, level: int = 2) -> Dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
    }


def adf_bullet_list(items: List[str]) -> Dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": item}],
                    }
                ],
            }
            for item in items
        ],
    }


def build_adf_description(
    description: str, acceptance_criteria: List[str]
) -> Dict[str, Any]:
    """
    Build a simple, readable Jira Cloud ADF document.
    """
    content = [
        adf_heading("Description", level=2),
        adf_text_paragraph(description),
        adf_heading("Acceptance Criteria", level=2),
        adf_bullet_list(acceptance_criteria),
    ]

    return {
        "type": "doc",
        "version": 1,
        "content": content,
    }


# -----------------------------
# Jira client
# -----------------------------


class JiraClient:
    def __init__(self, credentials: JiraCredentials, timeout_seconds: int = 30) -> None:
        self.base_url = credentials.base_url
        self.auth = HTTPBasicAuth(credentials.email, credentials.api_token)
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def create_issue(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/rest/api/3/issue"
        response = self.session.post(
            url,
            json={"fields": fields},
            timeout=self.timeout_seconds,
        )

        if response.status_code not in (200, 201):
            response_text = truncate(response.text)
            raise RuntimeError(
                f"Jira create issue failed: HTTP {response.status_code} - {response_text}"
            )

        return response.json()


# -----------------------------
# Payload building
# -----------------------------


def build_issue_fields(
    config: AppConfig,
    epic_key: str,
    issue: Dict[str, Any],
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "project": {"key": config.project_key},
        "issuetype": {"name": config.issue_type},
        "summary": issue["summary"],
        "description": build_adf_description(
            description=issue["description"],
            acceptance_criteria=issue["acceptance_criteria"],
        ),
        "labels": issue["labels"],
    }

    if config.epic_field_mode == "parent":
        # Preferred default for modern Jira Cloud setups.
        fields["parent"] = {"key": epic_key}
    elif config.epic_field_mode == "custom_field":
        # Fallback for setups that still require a specific Epic Link custom field.
        assert config.epic_link_field_id is not None
        fields[config.epic_link_field_id] = epic_key
    else:
        raise ValueError(f"Unsupported epic_field_mode: {config.epic_field_mode}")

    return fields


# -----------------------------
# Main program
# -----------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Jira issues from a JSON task file."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to local config JSON file",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to input JSON file containing epic_name and issues",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        credentials = load_env_credentials()
        config = load_config(args.config)
        input_data = load_json_file(args.input)
        epic_name, issues = validate_input_payload(input_data)
    except ValueError as exc:
        log_error(str(exc))
        return 2

    epic_key = config.epic_name_to_key.get(epic_name)
    if not epic_key:
        log_error(
            f"No Jira epic mapping found for epic_name '{epic_name}' in config.epic_name_to_key"
        )
        return 2

    log_info(f"Project key: {config.project_key}")
    log_info(f"Issue type: {config.issue_type}")
    log_info(f"Epic name: {epic_name}")
    log_info(f"Mapped epic key: {epic_key}")
    log_info(f"Epic field mode: {config.epic_field_mode}")
    log_info(f"Issues to create: {len(issues)}")

    jira = JiraClient(credentials=credentials)

    success_count = 0
    failure_count = 0

    for index, issue in enumerate(issues, start=1):
        summary = issue["summary"]
        log_info(f"[{index}/{len(issues)}] Creating issue: {summary}")

        try:
            fields = build_issue_fields(config=config, epic_key=epic_key, issue=issue)
            result = jira.create_issue(fields=fields)

            issue_key = result.get("key", "<unknown>")
            issue_id = result.get("id", "<unknown>")
            log_success(f"Created issue '{summary}' as {issue_key} (id={issue_id})")
            success_count += 1

        except Exception as exc:
            log_error(f"Failed to create '{summary}': {exc}")
            failure_count += 1
            continue

    log_info("Finished processing.")
    log_info(f"Successes: {success_count}")
    log_info(f"Failures: {failure_count}")

    return 1 if failure_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
