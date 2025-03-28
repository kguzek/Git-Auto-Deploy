"""Coding request parser module."""

import json

from .base import WebhookRequestParserBase


class CodingRequestParser(WebhookRequestParserBase):
    """Request parser for Coding"""

    def get_matching_projects(self, request_headers, request_body, action):
        """Gets a list of projects that match the incoming Coding web hook request"""

        data = json.loads(request_body)

        repo_urls = []

        coding_event = (
            "x-coding-event" in request_headers and request_headers["x-coding-event"]
        )
        action.log_info(f"Received '{coding_event}' event from Coding")

        if "repository" not in data:
            action.log_error("Unable to recognize data format")
            return []

        # One repository may posses multiple URLs for different protocols
        for k in ["web_url", "https_url", "ssh_url"]:
            if k in data["repository"]:
                repo_urls.append(data["repository"][k])

        # Get a list of configured repositories that matches the incoming web hook reqeust
        items = self.get_matching_repo_configs(repo_urls, action)

        repo_configs = []
        for repo_config in items:
            if "secret-token" in repo_config:
                if "token" not in data or not self.verify_token(
                    repo_config["secret-token"], data["token"]
                ):
                    action.log_warning(
                        f"Request token does not match the 'secret-token' "
                        f"configured for repository {repo_config["url"]}."
                    )
                    continue

            repo_configs.append(repo_config)

        return repo_configs

    def verify_token(self, secret_token, request_token):
        """Verifies the token sent by Coding"""
        return secret_token == request_token
