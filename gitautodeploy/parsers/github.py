"""GitHub request parser module"""

import hashlib
import hmac
import json

from .base import WebhookRequestParserBase


class GitHubRequestParser(WebhookRequestParserBase):
    """Request parser for GitHub"""

    def get_matching_projects(self, request_headers, request_body, action):
        """Gets a list of projects that match the incoming GitHub web hook request"""

        data = json.loads(request_body)

        repo_urls = []

        github_event = (
            "x-github-event" in request_headers and request_headers["x-github-event"]
        )

        action.log_info(f"Received '{github_event}' event from GitHub")

        if "repository" not in data:
            action.log_error("Unable to recognize data format")
            return []

        # One repository may posses multiple URLs for different protocols
        for k in ["url", "git_url", "clone_url", "ssh_url"]:
            if k in data["repository"]:
                repo_urls.append(data["repository"][k])

        # Get a list of configured repositories that matches the incoming web hook reqeust
        repo_configs = self.get_matching_repo_configs(repo_urls, action)

        return repo_configs

    def validate_request(self, request_headers, request_body, repo_configs, action):
        """Validates the incoming GitHub webhook request"""

        for repo_config in repo_configs:

            if "secret-token" not in repo_config:
                continue
            if "x-hub-signature" not in request_headers:
                action.log_info(
                    f"Request signature is missing for repository {repo_config["url"]}."
                )
                return False
            signature_valid = self.verify_signature(
                repo_config["secret-token"],
                request_body,
                request_headers["x-hub-signature"],
            )
            action.log_info(f"Signature is {"valid" if signature_valid else "invalid"}")
            if not signature_valid:
                action.log_info(
                    f"Request signature does not match the 'secret-token' "
                    f"configured for repository {repo_config["url"]}."
                )
                return False

        return True

    def verify_signature(self, token, body, signature):
        """Verify the signature of the incoming request"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        result = (
            "sha1=" + hmac.new(token.encode("utf-8"), body, hashlib.sha1).hexdigest()
        )
        return result == signature
