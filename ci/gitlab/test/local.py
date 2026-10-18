#!/usr/bin/env python3
"""Runs the template's .spitfire-deploy script against a live Spitfire, as a
GitLab runner would: in alpine:3.20 (busybox sh) with the job's variables and
the CI_* ones. Needs docker, SPITFIRE_URL and SPITFIRE_ADMIN_EMAIL /
SPITFIRE_ADMIN_PASSWORD (a Spitfire docs-shots stack:
STACK_RUN='python3 …/ci/gitlab/test/local.py' bash scripts/docs-shots/run.sh in
the Spitfire repo). A localhost controller is reached as host.docker.internal."""
import json, os, subprocess, sys, tempfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
import yaml  # noqa: E402

tpl = yaml.safe_load(open(os.path.join(HERE, "..", "spitfire.gitlab-ci.yml")))
job = tpl[".spitfire-deploy"]
URL = os.environ["SPITFIRE_URL"].rstrip("/")
API = URL + "/api/v1"
IN_DOCKER = URL.replace("://localhost", "://host.docker.internal").replace("://127.0.0.1", "://host.docker.internal")
fails = 0


def call(method, path, body=None, token=None):
    req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def check(name, ok, detail=""):
    global fails
    print(("ok    " if ok else "FAIL  ") + name + ("" if ok else f"  {detail}"))
    fails += 0 if ok else 1


tok = call("POST", "/auth/login", {"email": os.environ["SPITFIRE_ADMIN_EMAIL"], "password": os.environ["SPITFIRE_ADMIN_PASSWORD"]})["data"]["accessToken"]
pat = call("POST", "/me/api-tokens", {"name": "gitlab template test", "expiresInDays": 1}, tok)["data"]["token"]


def run_job(variables, job_vars=True):
    """The script in the job's image; job_vars False is the !reference form
    (only what the caller's job sets). Returns (exit code, dotenv, output)."""
    work = tempfile.mkdtemp(prefix="sf-gitlab-")
    env = {"CI_COMMIT_SHORT_SHA": "abc1234", "CI_PROJECT_PATH": "group/shop"}
    if job_vars:
        env.update({k: str(v) for k, v in job["variables"].items()})
    env.update(variables)
    args = ["docker", "run", "--rm", "-v", work + ":/builds", "-w", "/builds", "--add-host", "host.docker.internal:host-gateway"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    r = subprocess.run(args + [job["image"], "sh", "-c", "\n".join(job["script"])], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    sys.stdout.write("".join("        " + l + "\n" for l in out.splitlines()[-4:]))
    p = os.path.join(work, "spitfire-deploy.env")
    dotenv = dict(l.split("=", 1) for l in open(p).read().splitlines()) if os.path.exists(p) else {}
    return r.returncode, dotenv, out


def deploy(i):
    return next((d for d in call("GET", "/deploys", None, tok)["data"] if d["id"] == i), None)


check("template: a hidden job in .post with a dotenv report",
      job["stage"] == ".post" and job["artifacts"]["reports"]["dotenv"] == "spitfire-deploy.env")

# 1. all the monitors the token's user may change; the version and note default from CI
code, env, out = run_job({"SPITFIRE_URL": IN_DOCKER, "SPITFIRE_TOKEN": pat})
check("deploy: job passes, an id and woken monitors", code == 0 and len(env.get("SPITFIRE_DEPLOY_ID", "")) == 36
      and int(env.get("SPITFIRE_DEPLOY_WOKEN", "0")) >= 1, (code, env, out))
d = deploy(env.get("SPITFIRE_DEPLOY_ID"))
check("deploy: version and note default to the short SHA and the project", d and d["version"] == "abc1234" and d["note"] == "group/shop", d)

# 2. given version and a note with quotes, a backslash and a newline
code, env, _ = run_job({"SPITFIRE_URL": IN_DOCKER + "/", "SPITFIRE_TOKEN": pat, "SPITFIRE_DEPLOY_VERSION": "v9.9.9",
                        "SPITFIRE_DEPLOY_NOTE": 'say "hi"\\\nnow'})
d = deploy(env.get("SPITFIRE_DEPLOY_ID"))
check("deploy: the version and note arrive as written", code == 0 and d and d["version"] == "v9.9.9" and d["note"] == 'say "hi"\\ now', (code, d))

# 3. listed monitor ids wake only those
mons = call("GET", "/monitors", None, tok)["data"]
mons = mons.get("items", mons) if isinstance(mons, dict) else mons
on = [m["id"] for m in mons if m.get("enabled")]
code, env, out = run_job({"SPITFIRE_URL": IN_DOCKER, "SPITFIRE_TOKEN": pat, "SPITFIRE_DEPLOY_MONITORS": on[0]})
check("deploy, one monitor id: 1 woken", code == 0 and env.get("SPITFIRE_DEPLOY_WOKEN") == "1", (code, env, out))

# 4. an environment no monitor has wakes none, still announced
code, env, _ = run_job({"SPITFIRE_URL": IN_DOCKER, "SPITFIRE_TOKEN": pat, "SPITFIRE_ENVIRONMENT": "no-such-env"})
check("deploy, unknown environment: announced, 0 woken", code == 0 and env.get("SPITFIRE_DEPLOY_WOKEN") == "0", (code, env))

# 5. the !reference form: only SPITFIRE_URL and the token set on the caller's job
code, env, out = run_job({"SPITFIRE_URL": IN_DOCKER, "SPITFIRE_TOKEN": pat}, job_vars=False)
check("!reference form: works without the template's variables", code == 0 and len(env.get("SPITFIRE_DEPLOY_ID", "")) == 36, (code, out))

# 6. errors fail the job
code, env, out = run_job({"SPITFIRE_URL": IN_DOCKER, "SPITFIRE_TOKEN": "spf_wrong"})
check("wrong token: job fails with the HTTP status", code == 1 and "HTTP 401" in out and not env, (code, out))
code, _, out = run_job({"SPITFIRE_URL": IN_DOCKER})
check("no token: job fails", code == 1 and "SPITFIRE_TOKEN" in out, (code, out))
code, _, out = run_job({"SPITFIRE_URL": IN_DOCKER, "SPITFIRE_TOKEN": pat, "SPITFIRE_DEPLOY_MONITORS": "00000000-0000-0000-0000-000000000000"})
check("unknown monitor id: job fails", code == 1 and "HTTP 404" in out, (code, out))

sys.exit(1 if fails else 0)
