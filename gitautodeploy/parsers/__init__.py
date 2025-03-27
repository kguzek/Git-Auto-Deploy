"""Determines the origin of the incoming request and returns the appropriate request parser."""

import json

from .bitbucket import BitBucketRequestParser
from .github import GitHubRequestParser
from .gitlab import GitLabRequestParser
from .gitlabci import GitLabCIRequestParser
from .generic import GenericRequestParser
from .harbor import HarborRequestParser
from .coding import CodingRequestParser


def get_service_handler(request_headers, request_body, action, config):
    """Parses the incoming request and attempts to determine whether
    it originates from GitHub, GitLab or any other known service."""

    payload = json.loads(request_body)

    if not isinstance(payload, dict):
        raise ValueError("Invalid JSON object")

    user_agent = "user-agent" in request_headers and request_headers["user-agent"]
    content_type = "content-type" in request_headers and request_headers["content-type"]

    # Assume Coding if the X-Coding-Event HTTP header is set
    if "x-coding-event" in request_headers:
        return CodingRequestParser(config)

    # Assume GitLab if the X-Gitlab-Event HTTP header is set
    if "x-gitlab-event" in request_headers:

        # Special Case for Gitlab CI

        return (
            GitLabCIRequestParser(config)
            if content_type == "application/json" and "build_status" in payload
            else GitLabRequestParser(config)
        )

    # Assume GitHub if the X-GitHub-Event HTTP header is set
    if "x-github-event" in request_headers:
        return GitHubRequestParser(config)

    # Assume BitBucket if the User-Agent HTTP header is set to
    # 'Bitbucket-Webhooks/2.0' (or something similar)
    if user_agent and user_agent.lower().find("bitbucket") != -1:
        return BitBucketRequestParser(config)

    # Harbor Docker Container Registry webhooks
    if (
        isinstance(payload, dict)
        and "type" in payload
        and payload["type"] == "PUSH_ARTIFACT"
    ):
        return HarborRequestParser(config)

    # This handles old GitLab requests and Gogs requests for example.
    if content_type == "application/json":
        action.log_info("Received event from unknown origin.")
        return GenericRequestParser(config)

    action.log_error(
        "Unable to recognize request origin. Don't know how to handle the request."
    )
    return None
