"""Module containing the abstract parent class for git service parsers."""


class WebhookRequestParserBase:
    """Abstract parent class for git service parsers. Contains helper
    methods."""

    def __init__(self, config):
        self._config = config

    def get_matching_repo_configs(self, urls, action):
        """Iterates over the various repo URLs provided as argument (git://,
        ssh:// and https:// for the repo) and compare them to any repo URL
        specified in the config"""

        configs = []
        for url in urls:
            for repo_config in self._config["repositories"]:
                if repo_config in configs:
                    continue
                if repo_config.get("match-url", repo_config.get("url")) == url:
                    configs.append(repo_config)
                elif (
                    "url_without_usernme" in repo_config
                    and repo_config["url_without_usernme"] == url
                ):
                    configs.append(repo_config)

        if len(configs) == 0:
            action.log_warning(
                f"The URLs references in the webhook did not match any repository entry in "
                f"the config. For this webhook to work, make sure you have at least one "
                f"repository configured with one of the following URLs: {urls}"
            )

        return configs

    def validate_request(self, request_headers, request_body, repo_configs, action):
        """Only validates the request if the repository has no secret-tokens defined"""
        return not any("secret-token" in repo_config for repo_config in repo_configs)
