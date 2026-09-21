"""Serve the report over HTTP, with a rescan and the lineage view.

The same Jinja templates that `report` writes to files, rendered with ``served=True``.
There is no second page: what a browser gets here and what lands in the HTML file are the
same markup, the same styles and the same context builders. The served mode adds exactly
two things a file cannot have — a rescan that re-reads the source, and a lineage view
drawn on demand for whichever object you are looking at.

A model changes while you are editing it in Power BI Desktop, so ``serve`` holds a loader
rather than a Model and calls it again on rescan.

TWO SHAPES
----------
`create_app` serves one model at the root. `create_workspace_app` serves a directory: the
workspace index at ``/`` and each model at ``/model/<slug>/``. The second exists because a
model page under a workspace is the only page that can say a column is kept alive by some
*other* report in the directory, and that is the whole reason the workspace scan exists.

Moving a page off the root is what made `api_base` necessary. The page used to fetch a
relative ``api/lineage``, which resolves under whatever path it is served at — correct at
the root and wrong everywhere else. It is now told where its API is.

`Switchboard` sits in front of both. It holds whichever app is showing the current source and
answers the picker's own routes itself, so a bare `serve` can choose what to scan from the page
(`render/picker.py`) without either app knowing it can be swapped out.

WHY A LOOPBACK SERVER STILL NEEDS TWO CHECKS
--------------------------------------------
Binding to 127.0.0.1 keeps the network out; it does not keep a web page out. A site the
developer happens to visit can point its own hostname at 127.0.0.1 (DNS rebinding) and
then read every page here as same-origin — column names, measure DAX, RLS filters. The
Host header is what distinguishes that request from a real one, so it is checked. And a
plain cross-site form post needs no rebinding at all to reach ``/api/rescan``, so mutating
routes also require that the browser did not call them from another site.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from dax_quax.analysis.usage import Thresholds
from dax_quax.errors import DaxQuaxError
from dax_quax.model import Model
from dax_quax.render.editor import DEFAULT_EDITOR
from dax_quax.render.graph import render_lineage_svg
from dax_quax.render.report import TEMPLATES, build_context

# fastapi is imported at module scope on purpose. With `from __future__ import
# annotations` every annotation is a string, and FastAPI resolves a route's types against
# the MODULE namespace — so a Request imported inside create_app is invisible to it, and
# `request: Request` silently becomes a required query parameter (a 422 on every page).
# This module is only imported by `serve`, which is behind the [serve] extra.

__all__ = [
    "LOCAL_HOSTS",
    "State",
    "WorkspaceState",
    "Switchboard",
    "create_app",
    "create_switchboard",
    "create_workspace_app",
    "serve",
    "serve_switchboard",
    "serve_workspace",
]

Loader = Callable[[], Model]

#: Host headers a browser sends for a server on loopback. Starlette compares the header
#: with the port already stripped, so these carry no port. Anything else — including the
#: attacker-controlled name a rebound DNS record resolves to — is refused.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

#: `Sec-Fetch-Site` values that mean the request did not come from another site. A request
#: with no such header is allowed: curl and the older browsers send none, and the Host
#: check above is what stands between this server and a page that cannot be trusted.
_SAME_SITE = frozenset({"same-origin", "same-site", "none"})


def _guard(app: FastAPI, host: str | None) -> None:
    """Refuse requests whose Host is not this server's, and cross-site mutations."""
    allowed = [*LOCAL_HOSTS, *([host] if host and host not in LOCAL_HOSTS else [])]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    @app.middleware("http")
    async def _same_site_only(request: Request, call_next):  # noqa: ANN001, ANN202
        site = request.headers.get("sec-fetch-site")
        if request.method not in ("GET", "HEAD", "OPTIONS") and site and site not in _SAME_SITE:
            return Response(status_code=403)
        return await call_next(request)


@dataclass(slots=True)
class State:
    """What the server is currently showing. Replaced wholesale by a rescan."""

    loader: Loader
    description: str = "source"
    thresholds: Thresholds | None = None
    editor: str = DEFAULT_EDITOR
    switchable: bool = False
    model: Model | None = None
    lineage: Any = None
    loaded_at: _dt.datetime | None = None
    error: str | None = None
    scans: int = field(default=0)

    def rescan(self) -> None:
        """Re-read the source. A failure is kept and shown, not raised at the browser."""
        from dax_quax.analysis.lineage import build_lineage

        try:
            model = self.loader()
            self.model = model
            self.lineage = build_lineage(model)
            self.error = None
        except Exception as exc:  # noqa: BLE001 - surfaced in the page, not swallowed
            self.error = f"{type(exc).__name__}: {exc}"
        self.loaded_at = _dt.datetime.now(_dt.UTC)
        self.scans += 1

    def context(self) -> dict[str, Any]:
        if self.model is None:
            self.rescan()
        if self.model is None:
            raise RuntimeError(self.error or "no model loaded")
        context = build_context(
            self.model, lineage=self.lineage, thresholds=self.thresholds, editor=self.editor
        )
        context.update(
            served=True,
            switchable=self.switchable,
            source_description=self.description,
            scans=self.scans,
            load_error=self.error,
            loaded_at=(
                self.loaded_at.strftime("%H:%M:%S UTC") if self.loaded_at else "never"
            ),
        )
        return context


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES))
    # Same reason as render_report: select_autoescape matches on filename suffix and
    # every template here ends .j2, which would leave escaping off.
    templates.env.autoescape = True
    templates.env.trim_blocks = True
    templates.env.lstrip_blocks = True
    return templates


def create_app(
    loader: Loader,
    *,
    description: str = "source",
    thresholds: Thresholds | None = None,
    editor: str = DEFAULT_EDITOR,
    host: str | None = None,
    switchable: bool = False,
) -> FastAPI:
    state = State(
        loader=loader,
        description=description,
        thresholds=thresholds,
        editor=editor,
        switchable=switchable,
    )
    templates = _templates()

    app = FastAPI(title="dax-quax", docs_url=None, redoc_url=None)
    _guard(app, host)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        return templates.TemplateResponse(
            request, "report.html.j2", state.context()
        )

    @app.get("/api/lineage", response_class=HTMLResponse)
    def lineage(
        key: str,
        radius: int = Query(default=2, ge=1, le=4),
        direction: str = Query(default="both", pattern="^(up|down|both)$"),
    ) -> str:
        if state.lineage is None:
            state.rescan()
        if state.lineage is None:
            return '<div class="empty"><div class="head">No model loaded.</div></div>'
        return render_lineage_svg(state.lineage, key, radius=radius, direction=direction)  # type: ignore[arg-type]

    @app.post("/api/rescan")
    def rescan() -> Response:
        state.rescan()
        # 204 and let the client reload: the whole page changes after a rescan, and
        # swapping fragments would leave stale totals in the header.
        return Response(status_code=204 if state.error is None else 500)

    app.state.dax_quax = state
    return app


def serve(
    loader: Loader,
    *,
    description: str = "source",
    thresholds: Thresholds | None = None,
    editor: str = DEFAULT_EDITOR,
    host: str = "127.0.0.1",
    port: int = 8777,
    open_browser: bool = True,
) -> None:
    """Run the app. Binds to localhost only — this exposes model metadata."""
    import uvicorn

    app = create_app(
        loader, description=description, thresholds=thresholds, editor=editor, host=host
    )
    url = f"http://{host}:{port}/"
    print(f"dax-quax serving {description} at {url}")
    print("  the model is re-read on every rescan, so leave it open while you edit")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


# -- a whole workspace -------------------------------------------------------------------


@dataclass(slots=True)
class WorkspaceState:
    """The served equivalent of `render_workspace`. Replaced wholesale by a rescan.

    A workspace scan costs much more than a single model — every model, plus every report
    matched against it — so the per-model lineage graphs are built on demand and kept,
    rather than rebuilt per request the way a one-model server can afford to.
    """

    loader: Callable[[], Any]
    description: str = "workspace"
    thresholds: Thresholds | None = None
    editor: str = DEFAULT_EDITOR
    switchable: bool = False
    workspace: Any = None
    slugs: dict[str, str] = field(default_factory=dict)
    by_slug: dict[str, str] = field(default_factory=dict)
    lineages: dict[str, Any] = field(default_factory=dict)
    loaded_at: _dt.datetime | None = None
    error: str | None = None
    scans: int = field(default=0)

    def rescan(self) -> None:
        """Re-read the directory. A failure is kept and shown, not raised at the browser."""
        from dax_quax.render.workspace import slug_map

        try:
            workspace = self.loader()
            self.workspace = workspace
            self.slugs = slug_map(workspace.models)
            self.by_slug = {slug: name for name, slug in self.slugs.items()}
            self.lineages = {}
            self.error = None
        except Exception as exc:  # noqa: BLE001 - surfaced in the page, not swallowed
            self.error = f"{type(exc).__name__}: {exc}"
        self.loaded_at = _dt.datetime.now(_dt.UTC)
        self.scans += 1

    def ensure(self) -> Any:
        if self.workspace is None:
            self.rescan()
        if self.workspace is None:
            raise RuntimeError(self.error or "no workspace loaded")
        return self.workspace

    def lineage_for(self, name: str) -> Any:
        from dax_quax.analysis.lineage import build_lineage

        if name not in self.lineages:
            self.lineages[name] = build_lineage(self.ensure().models[name])
        return self.lineages[name]

    def index_context(self) -> dict[str, Any]:
        from dax_quax.render.workspace import build_workspace_context

        workspace = self.ensure()
        context = build_workspace_context(
            workspace,
            thresholds=self.thresholds,
            href=lambda name: f"/model/{self.slugs[name]}/",
        )
        context.update(
            served=True,
            switchable=self.switchable,
            scans=self.scans,
            load_error=self.error,
            loaded_at=self._stamp(),
        )
        return context

    def model_context(self, slug: str) -> dict[str, Any]:
        workspace = self.ensure()
        name = self.by_slug[slug]
        context = build_context(
            workspace.models[name],
            lineage=self.lineage_for(name),
            thresholds=self.thresholds,
            title=f"{name} — model cost and usage",
            # The workspace knows about reports this model has never seen, so its scope
            # travels with it. Otherwise this page claims a confidence the index withheld.
            scope=workspace.scope_for(name),
            back=("/", "workspace"),
            editor=self.editor,
        )
        context.update(
            served=True,
            switchable=self.switchable,
            # This page is not at the root, so a relative "api/" would resolve under it.
            api_base=f"/model/{slug}/api/",
            source_description=self.description,
            scans=self.scans,
            load_error=self.error,
            loaded_at=self._stamp(),
        )
        return context

    def _stamp(self) -> str:
        return self.loaded_at.strftime("%H:%M:%S UTC") if self.loaded_at else "never"


def create_workspace_app(
    loader: Callable[[], Any],
    *,
    description: str = "workspace",
    thresholds: Thresholds | None = None,
    editor: str = DEFAULT_EDITOR,
    host: str | None = None,
    switchable: bool = False,
) -> FastAPI:
    """The same two templates `report --workspace` writes, served instead of saved."""
    state = WorkspaceState(
        loader=loader,
        description=description,
        thresholds=thresholds,
        editor=editor,
        switchable=switchable,
    )
    templates = _templates()
    app = FastAPI(title="dax-quax workspace", docs_url=None, redoc_url=None)
    _guard(app, host)

    def _model(slug: str) -> str:
        state.ensure()
        if slug not in state.by_slug:
            raise HTTPException(status_code=404, detail=f"no model at {slug!r}")
        return state.by_slug[slug]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        return templates.TemplateResponse(request, "workspace.html.j2", state.index_context())

    @app.get("/model/{slug}/", response_class=HTMLResponse)
    def model_page(request: Request, slug: str) -> Any:
        _model(slug)
        return templates.TemplateResponse(request, "report.html.j2", state.model_context(slug))

    @app.get("/model/{slug}/api/lineage", response_class=HTMLResponse)
    def lineage(
        slug: str,
        key: str,
        radius: int = Query(default=2, ge=1, le=4),
        direction: str = Query(default="both", pattern="^(up|down|both)$"),
    ) -> str:
        name = _model(slug)
        return render_lineage_svg(
            state.lineage_for(name), key, radius=radius, direction=direction
        )

    @app.post("/model/{slug}/api/rescan")
    def rescan_model(slug: str) -> Response:
        # A model page's rescan re-reads the whole directory. A model on its own cannot
        # say whether another report started using one of its columns, and that question
        # is the only reason this page differs from `serve --pbip`.
        state.rescan()
        return Response(status_code=204 if state.error is None else 500)

    @app.post("/api/rescan")
    def rescan() -> Response:
        state.rescan()
        return Response(status_code=204 if state.error is None else 500)

    app.state.dax_quax = state
    return app


def serve_workspace(
    loader: Callable[[], Any],
    *,
    description: str = "workspace",
    thresholds: Thresholds | None = None,
    editor: str = DEFAULT_EDITOR,
    host: str = "127.0.0.1",
    port: int = 8777,
    open_browser: bool = True,
) -> None:
    """Run the workspace app. Localhost only — this exposes metadata for every model."""
    import uvicorn

    app = create_workspace_app(
        loader, description=description, thresholds=thresholds, editor=editor, host=host
    )
    url = f"http://{host}:{port}/"
    print(f"dax-quax serving {description} at {url}")
    print("  every model, against every report over it. Rescan re-reads the directory")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


# -- choosing the source from the page -------------------------------------------------


class OpenRequest(BaseModel):
    """What the picker page posts. Module scope, for the same reason as the imports."""

    kind: str
    path: str | None = None
    port: int | None = None


#: Paths the switchboard answers itself. Everything else goes to whichever app is showing
#: the current source, so the model and workspace apps above stay exactly as they were.
_PICKER_PATHS = frozenset({"/open", "/api/browse", "/api/open"})


class Switchboard:
    """Serve one source at a time, and let the page pick the next one.

    The two app shapes above differ in their routes, not just their data — a workspace
    has an index and a page per model — so a new source is a new app rather than a new
    loader. This holds whichever is current and routes to it, answering only the picker's
    own paths itself. Nothing is swapped until the new source has loaded: a pick that
    fails leaves the page you had.
    """

    def __init__(
        self,
        *,
        thresholds: Thresholds | None = None,
        editor: str = DEFAULT_EDITOR,
        host: str | None = None,
        start: str | None = None,
    ) -> None:
        self.thresholds = thresholds
        self.editor = editor
        self.host = host
        self.start = start
        self.current: Any = None
        self.description: str | None = None
        self.picker = self._picker_app()

    def show(self, shape: str, loader: Callable[[], Any], description: str) -> Any:
        """Build the app for a source. Returned rather than installed, so it can be vetted."""
        factory = create_workspace_app if shape == "workspace" else create_app
        return factory(
            loader,
            description=description,
            thresholds=self.thresholds,
            editor=self.editor,
            host=self.host,
            switchable=True,
        )

    def install(self, app: Any, description: str) -> None:
        self.current = app
        self.description = description

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        routed_to_source = (
            scope["type"] == "http"
            and self.current is not None
            and scope["path"] not in _PICKER_PATHS
        )
        await (self.current if routed_to_source else self.picker)(scope, receive, send)

    def _picker_app(self) -> FastAPI:
        from dax_quax.render import picker

        templates = _templates()
        app = FastAPI(title="dax-quax", docs_url=None, redoc_url=None)
        _guard(app, self.host)
        board = self

        @app.get("/")
        def root() -> Response:
            # Only reached when nothing is loaded yet; otherwise the current app has "/".
            return RedirectResponse("/open", status_code=303)

        @app.get("/open", response_class=HTMLResponse)
        def open_page(request: Request) -> Any:
            return templates.TemplateResponse(
                request,
                "open.html.j2",
                {
                    "title": "dax-quax — choose what to scan",
                    "current": board.description,
                    "start": board.start or str(pathlib.Path.home()),
                    "roots": picker.roots(),
                },
            )

        @app.get("/api/browse")
        def browse(path: str | None = None) -> Response:
            try:
                return JSONResponse(picker.listing(path or board.start or pathlib.Path.home()))
            except DaxQuaxError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        @app.post("/api/open")
        def open_source(choice: OpenRequest) -> Response:
            # A plain def: FastAPI runs it on a worker thread, so a slow scan does not
            # stall the page that is waiting on it.
            try:
                picked = picker.choose(choice.kind, choice.path, choice.port)
            except DaxQuaxError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            candidate = board.show(picked.shape, picked.loader, picked.description)
            state = candidate.state.dax_quax
            state.rescan()
            if state.error is not None:
                return JSONResponse({"error": state.error}, status_code=400)
            if picked.shape == "workspace" and not state.workspace.models:
                return JSONResponse(
                    {"error": f"no semantic models found under {choice.path}"},
                    status_code=400,
                )
            board.install(candidate, picked.description)
            return Response(status_code=204)

        return app


def create_switchboard(
    initial: tuple[str, Callable[[], Any], str] | None = None,
    *,
    thresholds: Thresholds | None = None,
    editor: str = DEFAULT_EDITOR,
    host: str | None = None,
    start: str | None = None,
) -> Switchboard:
    """A switchboard, optionally already showing ``(shape, loader, description)``.

    The initial source is not loaded here — the first page view loads it, as `serve`
    always has, so a slow source does not hold up the server starting.
    """
    board = Switchboard(thresholds=thresholds, editor=editor, host=host, start=start)
    if initial is not None:
        shape, loader, description = initial
        board.install(board.show(shape, loader, description), description)
    return board


def serve_switchboard(
    initial: tuple[str, Callable[[], Any], str] | None = None,
    *,
    thresholds: Thresholds | None = None,
    editor: str = DEFAULT_EDITOR,
    host: str = "127.0.0.1",
    port: int = 8777,
    open_browser: bool = True,
    start: str | None = None,
) -> None:
    """Run a switchboard, with the picker. Loopback only: the picker lists local folders.

    Bound anywhere else, a source must be given and is served on its own with no picker —
    a page that browses this machine's disk must never be reachable from another one.
    """
    import uvicorn

    if host not in LOCAL_HOSTS:
        if initial is None:
            raise DaxQuaxError(
                "the source picker lists folders on this machine, so it is only offered "
                "on localhost. Pass --pbip, --pbix, --workspace or --port to serve on "
                f"{host}."
            )
        shape, loader, description = initial
        run = serve_workspace if shape == "workspace" else serve
        run(
            loader,
            description=description,
            thresholds=thresholds,
            editor=editor,
            host=host,
            port=port,
            open_browser=open_browser,
        )
        return
    board = create_switchboard(
        initial, thresholds=thresholds, editor=editor, host=host, start=start
    )
    url = f"http://{host}:{port}/"
    if initial is None:
        print(f"dax-quax: choose what to scan at {url}")
    else:
        print(f"dax-quax serving {initial[2]} at {url}")
        print("  rescan re-reads it; 'change source' on the page picks something else")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    uvicorn.run(board, host=host, port=port, log_level="warning")
