#!/usr/bin/env python3
"""Spark: Gemini-powered Android project generator and self-healer.

Usage:
  spark.py generate   # reads PROMPT_TEXT, writes full project
  spark.py heal       # reads build_log.txt + project sources, rewrites broken files
"""
import json, os, re, sys, time
from pathlib import Path
import requests

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PREFERRED = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-3.8-flash").split(",") if m.strip()]
ROOT = Path.cwd().resolve()
TEXT_EXT = {".gradle", ".kts", ".kt", ".java", ".xml", ".properties", ".pro", ".toml"}

RULES = (
    "Hard requirements for every Android project you produce:\n"
    "- Groovy DSL files: settings.gradle, build.gradle, app/build.gradle, gradle.properties.\n"
    "- settings.gradle MUST contain pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } } "
    "and dependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); "
    "repositories { google(); mavenCentral() } } and include ':app'.\n"
    "- Root build.gradle uses the plugins {} block with 'apply false'. No buildscript/allprojects repositories.\n"
    "- gradle.properties: android.useAndroidX=true, org.gradle.jvmargs=-Xmx2048m, kotlin.code.style=official.\n"
    "- app/build.gradle sets namespace, compileSdk 34, minSdk 26, targetSdk 34, JVM target 17 "
    "(compileOptions + kotlinOptions jvmTarget '17').\n"
    "- Use Gradle 8.7, AGP 8.4.2, JDK 17. Do NOT include a Gradle wrapper, binary files, or images.\n"
    "- App icons: reference only @android:drawable/sym_def_app_icon or vector XML drawables you create.\n"
    "- Every referenced resource (strings, themes, drawables) must exist. Theme must be "
    "android:Theme.Material.NoActionBar or a theme you define in res/values/themes.xml.\n"
    "- Code must compile cleanly with no missing imports. Never use placeholder code.\n"
    "Output ONLY a raw JSON array of objects with string fields 'path' (relative) and 'content'. No markdown."
)


def log(msg):
    print(msg, flush=True)


def available_models():
    """Ask the API which text models this key can use, best candidates first."""
    names = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": API_KEY, "pageSize": 1000}, timeout=30)
        for m in r.json().get("models", []):
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            n = m["name"].split("/", 1)[-1]
            if not n.startswith("gemini") or re.search(r"image|tts|live|audio|embed|robotics|computer|nano|lite", n):
                continue
            names.append(n)
    except Exception as e:
        log(f"[!] Could not list models: {e}")

    def score(n):
        v = re.findall(r"gemini-(\d+(?:\.\d+)?)", n)
        ver = float(v[0]) if v else 0
        stable = 0 if re.search(r"preview|exp|latest", n) else 1
        return (ver, "flash" in n, stable)

    names.sort(key=score, reverse=True)
    log(f"[*] Models available to this key: {', '.join(names[:12]) or 'none'}")
    return names


def call_gemini(system, user):
    if not API_KEY:
        log("[-] GEMINI_API_KEY secret is missing or empty.")
        sys.exit(1)
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "responseSchema": {"type": "ARRAY", "items": {"type": "OBJECT",
                                 "properties": {"path": {"type": "STRING"}, "content": {"type": "STRING"}},
                                 "required": ["path", "content"]}},
                             "temperature": 0.2, "maxOutputTokens": 65536},
    }
    models = list(PREFERRED)
    avail = available_models()
    flash = [m for m in avail if "flash" in m]
    models += [m for m in flash + avail if m not in models][:8]
    for model in models:
        tries = 5 if model == models[0] else 3
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(1, tries + 1):
            try:
                r = requests.post(url, params={"key": API_KEY}, json=payload, timeout=420)
            except requests.RequestException as e:
                log(f"[!] {model} network error ({e}); retry {attempt}/{tries}")
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                    log(f"[+] Response from {model} ({len(text)} chars)")
                    return text
                except (KeyError, IndexError, ValueError):
                    log(f"[!] {model} returned no usable candidate: {r.text[:500]}")
                    break
            if r.status_code == 429 and re.search(r"quota|limit: 0|billing", r.text, re.I):
                log(f"[!] {model} quota exhausted for this key -> next model")
                break
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(90, 5 * 2 ** attempt)
                log(f"[!] {model} HTTP {r.status_code}; retry {attempt}/{tries} in {wait}s")
                time.sleep(wait)
                continue
            log(f"[!] {model} HTTP {r.status_code}: {r.text[:300]} -> trying next model")
            break
    log("[-] All Gemini models failed.")
    return None


def parse_files(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    def loads(t):
        try:
            return json.loads(t, strict=False)
        except json.JSONDecodeError:
            fixed = re.sub(r'\\(.)', lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else '\\\\' + m.group(1), t, flags=re.S)
            return json.loads(fixed, strict=False)
    try:
        data = loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        data = loads(text[start:end + 1])
    if isinstance(data, dict):
        data = data.get("files") or ([data] if "path" in data else [
            {"path": data["target_file"], "content": data["fixed_content"]}])
    return data


def write_files(files):
    written = 0
    for f in files:
        rel = str(f.get("path") or f.get("target_file") or "").strip().lstrip("./")
        content = f.get("content", f.get("fixed_content"))
        if not rel or content is None:
            continue
        dest = (ROOT / rel).resolve()
        if ROOT not in dest.parents or rel.startswith(".github") or rel.startswith(".git/"):
            log(f"[!] Skipping unsafe path: {rel}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        log(f"    wrote {rel}")
        written += 1
    return written


def project_sources(limit=120_000):
    out, total = [], 0
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT).as_posix()
        if not p.is_file() or p.suffix not in TEXT_EXT:
            continue
        if any(part in rel for part in ("/build/", ".gradle/", ".github/", ".git/")) or rel.startswith("build/"):
            continue
        body = p.read_text(errors="replace")
        total += len(body)
        if total > limit:
            break
        out.append(f"=== FILE: {rel} ===\n{body}")
    return "\n\n".join(out)


def generate():
    prompt = os.environ.get("PROMPT_TEXT", "").strip()
    if not prompt:
        log("[-] PROMPT_TEXT is empty.")
        sys.exit(1)
    system = "You are Spark, an autonomous senior Android engineer.\n" + RULES
    n = 0
    for attempt in range(1, 4):
        text = call_gemini(system, f"Generate the complete, compilable Android project for this spec:\n\n{prompt}")
        if not text:
            sys.exit(1)
        try:
            n = write_files(parse_files(text))
            break
        except Exception as e:
            log(f"[!] Could not parse model output (attempt {attempt}/3): {e}")
    else:
        log("[-] Model output unparseable after 3 attempts.")
        sys.exit(1)
    log(f"[+] Successfully wrote {n} files.")
    if n == 0 or not (ROOT / "settings.gradle").exists() and not (ROOT / "settings.gradle.kts").exists():
        log("[-] Generated project is missing settings.gradle.")
        sys.exit(1)


def heal():
    log_text = Path("build_log.txt").read_text(errors="replace") if Path("build_log.txt").exists() else ""
    errors = [l for l in log_text.splitlines() if re.search(r"^e: |error:|What went wrong|FAILURE|Unresolved|AAPT", l)]
    summary = "\n".join(errors[:80])
    tail = log_text[-6000:]
    system = ("You are Spark, an autonomous Android build doctor. Fix the compile errors in the project.\n"
              "Return ONLY the files you change, each as a COMPLETE file.\n" + RULES)
    user = (f"Gradle build failed.\n\nKey error lines:\n{summary}\n\nLog tail:\n{tail}\n\n"
            f"Current project files:\n{project_sources()}")
    text = call_gemini(system, user)
    if not text:
        return
    try:
        n = write_files(parse_files(text))
        log(f"[+] Self-healed {n} file(s).")
    except Exception as e:
        log(f"[!] Could not parse repair output: {e}")


if __name__ == "__main__":
    {"generate": generate, "heal": heal}[sys.argv[1] if len(sys.argv) > 1 else "heal"]()
