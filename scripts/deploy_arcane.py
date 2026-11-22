import os
import re
import sys
import json

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SUCCESS_STATUS_CODES = {200, 201, 204}
REQUEST_TIMEOUT = (30, 600)


def log_deploy_failure(operation, status_code, response_body, **context):
    print(f"ERROR: {operation} failed", file=sys.stderr)
    for key, value in context.items():
        print(f"  {key}: {value}", file=sys.stderr)
    if status_code is not None:
        print(f"  HTTP status: {status_code}", file=sys.stderr)
    print(f"  Response body: {response_body}", file=sys.stderr)


def parse_response_body(response):
    try:
        return response.json()
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
        return response.text


def request_or_exit(method, url, operation, api_key, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("X-Api-Key", api_key)
    try:
        response = requests.request(
            method, url, headers=headers, verify=False, timeout=REQUEST_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        log_deploy_failure(
            operation, None, f"{type(exc).__name__}: {exc}", url=url
        )
        print(
            f"ERROR: {operation} failed - request to Arcane could not be completed",
            file=sys.stderr,
        )
        sys.exit(1)

    if response.status_code not in SUCCESS_STATUS_CODES:
        log_deploy_failure(operation, response.status_code, parse_response_body(response), url=url)
        sys.exit(1)

    return response


def list_agents(arcane_url, api_key):
    """Return every environment (manager itself + agents), following pagination."""
    agents = []
    start = 0
    limit = 100
    while True:
        response = request_or_exit(
            "GET",
            f"{arcane_url}/api/agents",
            "fetch Arcane environments",
            api_key,
            params={"start": start, "limit": limit},
        )
        body = response.json()
        agents.extend(body.get("data") or [])
        pagination = body.get("pagination") or {}
        if not pagination.get("hasNextPage"):
            break
        start += limit
    return agents


def resolve_environment(arcane_url, api_key, environment_name):
    agents = list_agents(arcane_url, api_key)
    exact = [agent for agent in agents if agent.get("name") == environment_name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        print(
            f"ERROR: Multiple Arcane environments are named '{environment_name}'.",
            file=sys.stderr,
        )
        print(
            f"  Matching environments: {[(a.get('id'), a.get('name')) for a in exact]}",
            file=sys.stderr,
        )
        sys.exit(1)

    available = sorted(agent.get("name", "?") for agent in agents)
    print(f"ERROR: Environment '{environment_name}' not found in Arcane.", file=sys.stderr)
    print(f"  Available environments: {available}", file=sys.stderr)
    sys.exit(1)


def find_project(arcane_url, api_key, environment_id, stack_name):
    response = request_or_exit(
        "GET",
        f"{arcane_url}/api/agents/{environment_id}/projects",
        "fetch projects",
        api_key,
        params={"name": stack_name, "limit": 100},
    )
    projects = response.json().get("data") or []
    for project in projects:
        if project.get("name") == stack_name:
            return project
    return None


def parse_environment_file(environment_file, stack_file_content):
    """Return the .env lines whose variable is referenced as ${NAME} in the compose file."""
    if not environment_file or environment_file == "default":
        return None

    with open(environment_file, "r") as file:
        environment = file.read()

    used_lines = []
    for raw_line in environment.split("\n"):
        if not raw_line.strip() or "=" not in raw_line:
            continue
        name, value = raw_line.split("=", 1)
        name = name.strip()
        if not name or name.startswith("#"):
            continue
        if not re.search(r"\$\{" + re.escape(name) + r"\}", stack_file_content):
            continue
        value = value.replace("\n", "").replace("\r", "").replace("\t", "").replace("'", "")
        used_lines.append(f"{name}={value}")

    return "\n".join(used_lines) + "\n" if used_lines else ""


def create_project(arcane_url, api_key, environment_id, stack_name, compose_file_path, environment_file):
    compose_content = open(compose_file_path, "r").read()
    env_content = parse_environment_file(environment_file, compose_content)

    project = {"name": stack_name, "composeContent": compose_content}
    if env_content is not None:
        project["envContent"] = env_content

    # Arcane expects multipart/form-data with two JSON parts: the project config
    # and the initial workspace manifest (an empty change set is valid).
    files = {
        "project": (None, json.dumps(project), "application/json"),
        "manifest": (None, json.dumps({"fileChanges": []}), "application/json"),
    }

    print(f"Creating project '{stack_name}' (environment {environment_id})...")
    response = request_or_exit(
        "POST",
        f"{arcane_url}/api/agents/{environment_id}/projects",
        "create project",
        api_key,
        files=files,
    )
    return response.json().get("data") or {}


def update_project(arcane_url, api_key, environment_id, project_id, compose_file_path, environment_file):
    compose_content = open(compose_file_path, "r").read()
    env_content = parse_environment_file(environment_file, compose_content)

    payload = {"composeContent": compose_content}
    if env_content is not None:
        payload["envContent"] = env_content

    print(f"Updating project {project_id} with compose file {compose_file_path}...")
    response = request_or_exit(
        "PATCH",
        f"{arcane_url}/api/agents/{environment_id}/projects/{project_id}",
        "update project",
        api_key,
        json=payload,
    )
    return response.json().get("data") or {}


def deploy_project(arcane_url, api_key, environment_id, project_id, deploy_action, pull_policy):
    """Trigger a deploy and follow the newline-delimited JSON stream until it is done."""
    print(f"Deploying project {project_id} (action={deploy_action}, pull_policy={pull_policy})...")
    url = f"{arcane_url}/api/agents/{environment_id}/projects/{project_id}/deploy"
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    payload = {"pullPolicy": pull_policy}

    try:
        with requests.post(
            url,
            headers=headers,
            params={"action": deploy_action},
            json=payload,
            verify=False,
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status_code not in SUCCESS_STATUS_CODES:
                log_deploy_failure(
                    "deploy project",
                    response.status_code,
                    parse_response_body(response),
                    url=url,
                )
                sys.exit(1)

            error = None
            done = False
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    print(f"  {raw_line}")
                    continue
                if "log" in event:
                    print(f"  {event['log'].rstrip()}")
                elif "error" in event:
                    error = event["error"]
                elif event.get("done"):
                    done = True

            if error:
                log_deploy_failure("deploy project", None, error, url=url)
                sys.exit(1)
            if not done:
                log_deploy_failure(
                    "deploy project",
                    None,
                    "deploy stream ended without a completion event",
                    url=url,
                )
                sys.exit(1)
    except requests.RequestException as exc:
        log_deploy_failure("deploy project", None, f"{type(exc).__name__}: {exc}", url=url)
        sys.exit(1)


def main():
    if len(sys.argv) not in (5, 6, 7):
        print(f"Expected 4, 5 or 6 arguments but got {len(sys.argv) - 1}")
        print("Arguments received:", sys.argv)
        sys.exit(1)

    print("Starting Arcane deployment script...")

    arcane_url = sys.argv[1].rstrip("/")
    api_key = sys.argv[2]
    changed_files_path = sys.argv[3]
    environment_file = sys.argv[4]
    deploy_action = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else "up"
    pull_policy = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else "if_not_present"

    if not environment_file or environment_file == "default":
        environment_file = None

    if deploy_action not in ("up", "redeploy"):
        print(f"ERROR: Unsupported deploy_action '{deploy_action}' (use 'up' or 'redeploy').", file=sys.stderr)
        sys.exit(1)

    if not changed_files_path or not os.path.isfile(changed_files_path):
        print(f"Changed files path is invalid or file not found: {changed_files_path}")
        sys.exit(1)

    if environment_file and not os.path.isfile(environment_file):
        print(f"Environment file not found: {environment_file}")
        sys.exit(1)

    with open(changed_files_path, "r") as file:
        changed_files = file.readlines()

    deployed = []
    for file_path in changed_files:
        file_path = file_path.strip()
        if not file_path.endswith("docker-compose.yml"):
            continue

        parts = file_path.split("/")
        if len(parts) < 2:
            print(f"Skipping '{file_path}': expected <environment>/<stack>/docker-compose.yml")
            continue

        if not os.path.isfile(file_path):
            # A deleted stack: never destroy running containers from CI, but say
            # it loudly so the leftover project gets cleaned up by hand.
            print(
                f"WARNING: '{file_path}' no longer exists (stack removed from the repo) - "
                "skipping. Delete the Arcane project manually if the stack must go away.",
                file=sys.stderr,
            )
            continue

        environment_name = parts[0]
        stack_name = parts[1]
        print(f"Deploying {file_path} -> environment '{environment_name}', stack '{stack_name}'")

        environment = resolve_environment(arcane_url, api_key, environment_name)
        environment_id = environment["id"]
        project = find_project(arcane_url, api_key, environment_id, stack_name)

        if project:
            print(
                f"Project '{stack_name}' in environment '{environment_name}': "
                f"found ({project['id']}), updating"
            )
            project = update_project(
                arcane_url, api_key, environment_id, project["id"], file_path, environment_file
            )
        else:
            print(
                f"Project '{stack_name}' in environment '{environment_name}': "
                "not found, will create"
            )
            project = create_project(
                arcane_url, api_key, environment_id, stack_name, file_path, environment_file
            )

        deploy_project(
            arcane_url, api_key, environment_id, project["id"], deploy_action, pull_policy
        )
        print(f"Deployed project '{stack_name}' in environment '{environment_name}'")
        deployed.append(f"{environment_name}/{stack_name}")

    if not deployed:
        print("No docker-compose.yml changes to deploy.")
        return

    print(f"Done. Deployed {len(deployed)} stack(s): {', '.join(deployed)}")


if __name__ == "__main__":
    main()
