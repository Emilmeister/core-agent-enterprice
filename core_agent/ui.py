"""Serve only the packaged browser build; never fall back to the SPA for API paths."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route


UI_DIST = Path(__file__).parent / "ui_dist"
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HASHED = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+")


def _build_file(root, path):
    """Reject symlinks including intermediate directories and the build root."""
    try:
        relative = path.relative_to(root)
        current = root
        if current.is_symlink():
            return False
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        return path.is_file() and path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def ui_routes(settings, *, directory=None):
    """Return routes and their exact public GET/HEAD paths from a trusted build."""
    root = UI_DIST if directory is None else Path(directory)
    shell = root / "index.html"
    if settings is None or not settings.ui_client_id or not _build_file(root, shell):
        return [], frozenset()

    issuer = urlsplit(settings.issuer)
    origin = f"{issuer.scheme}://{issuer.netloc}"
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; font-src 'self'; "
            f"connect-src 'self' {origin}; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
        ),
    }
    files = {"/ui/": shell, "/ui/index.html": shell}
    assets = root / "assets"
    if not assets.is_symlink() and assets.is_dir():
        for path in assets.rglob("*"):
            relative = path.relative_to(root)
            if all(_FILENAME.fullmatch(part) for part in relative.parts) and _build_file(root, path):
                files["/ui/" + relative.as_posix()] = path

    async def serve(request):
        path = files[request.scope["path"]]
        # Recheck after enumeration: a missing/replaced build must not expose a target.
        if not _build_file(root, path):
            return PlainTextResponse("Not Found", status_code=404, headers=headers)
        response_headers = dict(headers)
        if path != shell and _HASHED.fullmatch(path.name):
            response_headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return FileResponse(path, headers=response_headers)

    async def redirect(_request):
        return RedirectResponse("/ui/", headers=headers)

    routes = [Route("/ui", redirect, methods=["GET", "HEAD"])]
    routes.extend(Route(path, serve, methods=["GET", "HEAD"]) for path in sorted(files))
    return routes, frozenset({"/ui", *files})
