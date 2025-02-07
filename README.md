# Git-Auto-Deploy

Fork by [@kguzek](https://github.com/kguzek)

## Why?

This repository is a fork of a fork of the original Git-Auto-Deploy created by [@olipo186](https://github.com/olipo186/Git-Auto-Deploy).

It is a great project with many features, including a web UI, realtime updates due to websocket interaction, multiple source listening, deployment script customisation and many commandline switches.

Unfortunately the project was started back on Christmas Eve in 2011 (13 years ago as of writing this README), which means some elements of it are outdated.
The most glaring issue is that it's written in Python 2, which although still being used today, is really deprecated in favour of Python 3.

That's where the upstream fork comes in -- the fork by [@phillip-peterson](https://github.com/philip-peterson/Git-Auto-Deploy) aims to add forward compatibility with Python 3. 
It was used to file a pull request in the original repo, which has been stale for the last half year.

So instead of using the original repository, I cloned Phillip's repo -- until I started encountering issues. It turns out the changes made were very minimal and don't really introduce Python 3 compatibility.

## Upgrading to Python 3

My fork generally cleans up the codebase a lot, replacing the `%` operator with f-strings, making the code easier to read by adding early returns, and adding Copilot-generated function, class and module docstrings. 
I also moved all imports to the top-level of the module, which improves performance as the module is not imported on each call of the function.

The real reason I needed to make changes in the first place was due to libraries like hkey and requests using bytes instead of strings, which changed from Python 2 to 3, and thus broke the program when trying to run it in a modern version.

My fork addresses all of these issues and additionally brings its own improvements.

## Other changes

### Security holes

By testing my changes with a GitHub webhook, I discovered a quite serious security vulnerability. The underlying mechanism which [determines which webhook handler](/gitautodeploy/parsers/__init__.py) to use makes its decision based on a couple factors:

- the user agent
- the content type header
- application-specific headers (such as X-Github-Event)

This correctly identifies legitimate requests made by apps such as GitHub, GitLab, Coding and BitBucket, and logs an error if none of these identifiers are present.

However, there is an exception made if the request's content type is `application/json` -- in this case, it uses a `GenericRequestParser`. The block comment suggests this is for handling old GitLab or Gogs requests.
This would be fine but the problem is that this specific implementation of the request parser does not support webhook secrets -- so if you configure your project to need secrets to be able to be pushed to,
this request parser will simply ignore that configuration and let all requests through, regardless of whether or not they contain the secret.

This means that in essence, all an attacker would need to do to trigger your deployment script would be to craft a POST request with a `Content-Type` header of `application/json` and an appropriate request body.

This completely defeats the point of having webhook secrets, and hence is why I patched this problem in my fork.
The method I chose was to simply add a condition that requests will be validated using the base webhook request parser if and only if no matching projects define a secret token.
If any repository matching the request has defined a secret token, the request will immediately fail. This is defined `validate_request` of [base.py](/gitautodeploy/parsers/base.py).

### Configuration options

I also added a couple new configuration options regarding the HTTP and Websocket server. They allow serving the websocket server without SSL -- before, not providing certificates caused the server to fail, but now it can successfully be reached at `ws://`. 
This is done by setting the new `ws-always-ssl` configuration option to `false`.

This allows you to serve the HTTP and WS servers without SSL whatsoever. This might sound like a bad idea, but my use case was that I'm running it behind a Cloudflare tunnel, and all requests would be proxied by the local network.
Outside requests would all still be made through SSL, just internally they are served as HTTP or TCP.

I introduced some more configuration options to support this:

- ws-public-uri
- http-public-uri

These are so that the web UI doesn't mistakenly report the internal host and port bindings to the end user -- e.g. the websocket server is hosted at ws://localhost:8009, but is proxied through a Cloudflare tunnel at https://ws.domain.com:443,
so the front-end websocket client cannot establish a connection. Setting these options can override the URL chosen by the web UI as well as the public-facing URLs displayed to the user.

## Contribution

If you have any other ideas or requests regarding the maintenance of this tool, I am very happy to discuss either by [opening an issue](https://github.com/kguzek/Git-Auto-Deploy/issues/new/choose) in this repository, [starting a discussion](https://github.com/kguzek/Git-Auto-Deploy/discussions/2), or contacting me directly. Thanks!

~ Konrad
