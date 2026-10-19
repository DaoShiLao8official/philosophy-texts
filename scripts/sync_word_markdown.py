"""Generate adjacent Markdown from tracked DOCX files using Pandoc 3.12.1."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import zipfile

FORMAT = "gfm+tex_math_dollars+footnotes"
STATE = ".github/word-markdown-state.json"


def nodes(value, tag):
    if isinstance(value, dict):
        if value.get("t") == tag:
            yield value
        for item in value.values():
            yield from nodes(item, tag)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item, tag)


def safe_emphasis(value):
    # HTML avoids literal ** / * at Chinese punctuation boundaries on GitHub.
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict) and item.get("t") == "Figure":
                # Plain image + caption stays readable and auditable in GFM.
                result.extend(safe_emphasis(item["c"][2]))
                result.extend(safe_emphasis(item["c"][1][1]))
            elif isinstance(item, dict) and item.get("t") in ("Strong", "Emph"):
                tag = "strong" if item["t"] == "Strong" else "em"
                result.append({"t": "RawInline", "c": ["html", "<" + tag + ">"]})
                result.extend(safe_emphasis(item["c"]))
                result.append({"t": "RawInline", "c": ["html", "</" + tag + ">"]})
            else:
                result.append(safe_emphasis(item))
        return result
    if isinstance(value, dict):
        return {key: safe_emphasis(item) for key, item in value.items()}
    return value


def pandoc_run(pandoc, args, data=None):
    result = subprocess.run(
        [pandoc, *args], input=data, encoding="utf-8", check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.stderr.strip():
        print(result.stderr.strip())
    return result.stdout


def relative_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe repository path: {relative}")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        raise ValueError(f"Path escapes the repository: {relative}")
    return target


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_roundtrip(original, converted, source):
    def text(ast):
        # Validate the body and each footnote independently.
        def visit(value):
            if isinstance(value, dict):
                if value.get("t") == "Str":
                    return "".join(value["c"].split())
                if value.get("t") == "Note":
                    return ""
                return "".join(visit(item) for item in value.values())
            if isinstance(value, list):
                return "".join(visit(item) for item in value)
            return ""
        return visit(ast["blocks"])
    if text(original) != text(converted):
        raise ValueError(f"Text did not survive Markdown conversion: {source}")
    for tag in ("Note", "Image", "Math", "Table"):
        if len(list(nodes(original, tag))) != len(list(nodes(converted, tag))):
            raise ValueError(f"{tag} count changed during conversion: {source}")
    if [n["c"] for n in nodes(original, "Math")] != [n["c"] for n in nodes(converted, "Math")]:
        raise ValueError(f"Formula changed during conversion: {source}")
    def notes(ast):
        return [text({"blocks": n["c"]}) for n in nodes(ast, "Note")]
    if notes(original) != notes(converted):
        raise ValueError(f"Footnote text changed during conversion: {source}")


def convert(root, relative, pandoc):
    source = relative_path(root, relative)
    output_rel = str(PurePosixPath(relative).with_suffix(".md"))
    output = relative_path(root, output_rel)
    ast = safe_emphasis(json.loads(pandoc_run(
        pandoc, ["-f", "docx", "-t", "json", "--track-changes=accept", str(source)])))
    media_rel = PurePosixPath(relative).parts[0] + "/_media" if "/" in relative else "_media"
    assets = {}
    with zipfile.ZipFile(source) as archive:
        for image in nodes(ast, "Image"):
            url = image["c"][2][0]
            if url.startswith(("https:", "http:")):
                continue
            internal = url if url in archive.namelist() else "word/" + url
            payload = archive.read(internal)
            name = sha256(payload)[:20] + PurePosixPath(url).suffix.lower()
            asset_rel = media_rel + "/" + name
            asset = relative_path(root, asset_rel)
            assets[asset_rel] = payload
            image["c"][2][0] = os.path.relpath(asset, output.parent).replace("\\", "/")
            image["c"][0] = ["", [], []]
    markdown = pandoc_run(pandoc, ["-f", "json", "-t", FORMAT, "--wrap=none"], json.dumps(ast))
    converted = json.loads(pandoc_run(pandoc, ["-f", FORMAT, "-t", "json"], markdown))
    validate_roundtrip(ast, converted, relative)
    return output_rel, markdown.encode("utf-8"), assets


def sync(root, pandoc):
    root = root.resolve()
    version = pandoc_run(pandoc, ["--version"]).splitlines()[0]
    if version != "pandoc 3.12.1":
        raise ValueError(f"Expected pandoc 3.12.1; found {version}")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
    documents = sorted(p for p in tracked if p.lower().endswith(".docx")
                       and not PurePosixPath(p).name.startswith("~$"))
    outputs = [str(PurePosixPath(p).with_suffix(".md")) for p in documents]
    if len(set(p.casefold() for p in outputs)) != len(outputs):
        raise ValueError("Two Word documents would overwrite the same Markdown file")
    state_path = relative_path(root, STATE)
    previous = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"documents": {}}
    current = {}
    updated = []
    removed = []
    # Validate the whole corpus before changing any published Markdown or assets.
    with tempfile.TemporaryDirectory(prefix="word-markdown-") as temporary:
        stage = Path(temporary)
        files = set()
        for source in documents:
            target, markdown, assets = convert(root, source, pandoc)
            current[source] = {"markdown": target, "sha256": sha256(markdown)}
            assets[target] = markdown
            for relative, payload in assets.items():
                staged = relative_path(stage, relative)
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(payload)
                files.add(relative)
            print(f"Converted: {source}")
        for source, entry in previous["documents"].items():
            if source in current:
                continue
            expected = str(PurePosixPath(source).with_suffix(".md"))
            if not source.lower().endswith(".docx") or entry["markdown"] != expected:
                raise ValueError(f"Invalid managed document: {source}")
            if expected in outputs:
                continue
            target = relative_path(root, expected)
            if target.exists():
                if sha256(target.read_bytes()) == entry["sha256"]:
                    removed.append(expected)
                else:
                    print(f"Retained manually edited Markdown after Word deletion: {expected}")
        for relative in sorted(files):
            target = relative_path(root, relative)
            payload = relative_path(stage, relative).read_bytes()
            if not target.exists() or target.read_bytes() != payload:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                updated.append(relative)
        for relative in removed:
            relative_path(root, relative).unlink()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"version": 1, "documents": current}, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n")
    print(f"Synced {len(documents)} Word documents; updated {len(updated)} files; removed {len(removed)} Markdown files.")
    return {"documents": len(documents), "updated": updated, "removed": removed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pandoc", default="pandoc")
    args = parser.parse_args()
    sync(args.root, args.pandoc)


if __name__ == "__main__":
    main()
