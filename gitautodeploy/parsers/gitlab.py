"""GitLab Webhook Request Parser."""

import json
from .base import WebhookRequestParserBase


class GitLabRequestParser(WebhookRequestParserBase):
    """GitLab Webhook Request Parser."""

    def get_matching_projects(self, request_headers, request_body, action):
        """Gets a list of projects that match the incoming GitLab web hook request"""

        data = json.loads(request_body)

        repo_urls = []

        gitlab_event = (
            "x-gitlab-event" in request_headers and request_headers["x-gitlab-event"]
        )

        action.log_info(f"Received '{gitlab_event}' event from GitLab")

        if "repository" not in data:
            action.log_error("Unable to recognize data format")
            return []

        # One repository may posses multiple URLs for different protocols
        for k in ["url", "git_http_url", "git_ssh_url"]:
            if k in data["repository"]:
                repo_urls.append(data["repository"][k])

        # Get a list of configured repositories that matches the incoming web hook reqeust
        repo_configs = self.get_matching_repo_configs(repo_urls, action)

        return repo_configs

    def validate_request(self, request_headers, request_body, repo_configs, action):

        for repo_config in repo_configs:

            # Validate secret token if present
            if "secret-token" in repo_config and "x-gitlab-token" in request_headers:

                if repo_config["secret-token"] != request_headers["x-gitlab-token"]:
                    action.log_info(
                        f"Request token does not match the 'secret-token' "
                        f"configured for repository {repo_config["url"]}."
                    )
                    return False

        return True
