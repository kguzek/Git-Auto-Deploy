"""Harbor Docker Container Registry request parser"""

import json

from .base import WebhookRequestParserBase


EVENT_KEYS = ["type", "occur_at", "operator", "event_data"]
RESOURCE_KEYS = ["digest", "tag", "resource_url"]
REPOSITORY_KEYS = ["date_created", "name", "namespace", "repo_full_name", "repo_type"]


def is_valid_webhook_request(data):
    """Validates the incoming webhook request data as per
    https://goharbor.io/docs/2.12.0/working-with-projects/project-configuration/configure-webhooks/#payload-format
    """
    return (
        isinstance(data, dict)
        and all(key in data for key in EVENT_KEYS)
        and all(key in data["event_data"] for key in ("resources, repository"))
        and isinstance(data["event_data"]["resources"], list)
        and isinstance(data["event_data"]["repository"], dict)
        and len(data["event_data"]["resources"]) > 0
        and all(
            key in resource
            for key in RESOURCE_KEYS
            for resource in data["event_data"]["resources"]
        )
        and all(key in data["event_data"]["repository"] for key in REPOSITORY_KEYS)
    )


class HarborRequestParser(WebhookRequestParserBase):
    """Request parser for Harbor Docker Container Registry"""

    def get_matching_projects(self, _request_headers, request_body, action):
        """Gets a list of projects that match the incoming Harbor webhook request"""

        data = json.loads(request_body)

        if not is_valid_webhook_request(data):
            action.log_error("Invalid webhook request data format")
            return []

        action.log_info(f"Received '{data["event_type"]}' event from Harbor")

        # Get a list of configured repositories that matches the incoming web hook reqeust
        match_url = data["event_data"]["resources"][0]["resource_url"].split("@")[0]
        repo_configs = self.get_matching_repo_configs([match_url], action)

        return repo_configs

    def validate_request(self, request_headers, request_body, repo_configs, action):
        """Validates the incoming GitHub webhook request"""

        for repo_config in repo_configs:

            if "secret-token" not in repo_config:
                continue
            auth_header = request_headers.get("Authorization")
            expected_auth_header = repo_config["secret-token"]
            if auth_header is None or auth_header != expected_auth_header:
                return False

        return True
