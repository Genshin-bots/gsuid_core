import contextlib

from fastapi import Request
from fastapi_amis_admin import admin as amis_admin
from starlette.responses import RedirectResponse
from fastapi_user_auth.admin import admin as user_auth_admin
from fastapi_user_auth.auth.auth import AuthRouter
from fastapi_amis_admin.amis.components import App, Tpl, Grid, Html, Page

from gsuid_core.webconsole.html import login_html, footer_html
from gsuid_core.webconsole.utils import overrides


@overrides(user_auth_admin)
def attach_page_head(page: Page) -> Page:
    page.body = [
        Html(html=login_html),
        Grid(
            columns=[
                {
                    "body": [page.body],
                    "lg": 3,
                    "md": 4,
                    "valign": "middle",
                }
            ],
            align="center",
            valign="middle",
        ),
    ]
    return page


@overrides(amis_admin.AdminApp)
async def _get_page_as_app(self, request: Request) -> App:
    app = App()
    app.brandName = self.site.settings.site_title
    app.header = Tpl(
        className="w-full",
        tpl="""
        <div class='flex justify-between'>
            <div>
                <a href='https://github.com/Genshin-bots/gsuid_core'
                target='_blank' title='Copyright'>
                    <i class='fa fa-github fa-2x'></i>
                </a>
            </div>
        </div>
        """,
    )  # type: ignore
    app.footer = footer_html
    children = await self.get_page_schema_children(request)
    app.pages = [{"children": children}] if children else []  # type: ignore
    return app


@property
@overrides(AuthRouter)
def route_logout(self):
    @self.auth.requires()
    async def user_logout(request: Request):
        token_value = request.auth.backend.get_user_token(request=request)
        with contextlib.suppress(Exception):
            await self.auth.backend.token_store.destroy_token(token=token_value)
        response = RedirectResponse(url="/genshinuid")
        response.delete_cookie("Authorization")
        return response

    return user_logout


amis_admin.AdminApp._get_page_as_app = _get_page_as_app
user_auth_admin.attach_page_head = attach_page_head
AuthRouter.route_logout = route_logout  # type:ignore
