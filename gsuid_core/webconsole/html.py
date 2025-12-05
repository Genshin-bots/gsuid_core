import fastapi_amis_admin

from gsuid_core.version import __version__ as gscore_version

bots = "HoshinoBot · NoneBot2 · Koishi · Yunzai · ZeroBot · AstrBot"
sup = f"✨支持连接{bots}的多功能Bot插件核心✨"

style = """
div {
  background: rgba(255, 255, 255, 0.6) url('/webstatic/bg.jpg');
  background-blend-mode: screen;
  background-repeat: no-repeat;
  background-attachment: fixed;
  background-size: cover;
}

html, body,
.app-wrapper {
    position: relative;
    width: 100%;
    height: 100%;
    margin: 0;
    padding: 0;
}

:root {
    --Page-main-bg: #ffffff;
    --colors-brand-main: #ce5050;
    --colors-brand-5: #ce5050;
    --colors-brand-4: #b84343;
    --colors-brand-6: #de5b5b;
    --Layout-asideLink-color: #ce5050;
    --colors-brand-10: rgba(206, 80, 80, 0.08)
    --Layout-nav-height: 2.8rem;
    --borders-radius-3: 10px;
    --Layout-aside-bg: rgba(0, 0, 0, 0);
    --sizes-size-7: 1rem;
}

.amis-scope {
    display: flex;
    justify-content: center;
    overflow: hidden; /* 防止滚动条 */
}

.amis-scope .a-Panel {
    /* margin: 1.5rem 3rem 1.5rem -1.5rem; */
    padding: 1.5rem 3rem 1.5rem 0rem;
    border-radius: 10px;
    background-color: rgba(0, 0, 0, 0);
    width: 150%;
    margin: 0 auto;
    margin-left: -25%;
}


rect {
    fill: #ffffff;
}

.amis-scope .a-TextControl .InputText-invisible {
    background-color: #ffffff;
}

.amis-scope .a-TextControl-icon-view svg {
    background-color: #ffffff;
}

.amis-scope .a-AppBcn {
    background-color: #FFFFFF;
}

.amis-scope .a-ButtonToolbar {
    border-radius: 10px;
    /* background-color: #ce5050; */
}

/*
.amis-scope .a-Layout--asideFixed .a-Layout-asideWrap  {
    box-shadow: 1px 8px 3px 2px rgba(26, 25, 23, 0.3);
}
*/

.amis-scope .a-TextControl .InputText-clear {
    background-color: #ffffff;
}

.amis-scope .a-Layout-brand, .amis-scope .a-Layout-brandBar, .amis-scope .a-Layout-aside {
    background-color: rgba(0, 0, 0, 0);
}

.amis-scope .a-AsideNav-list {
    background-color: #FFFFFF;
    border-radius: 8px; /* 圆角矩形，可根据需要调整数值 */
    color: #1a1917; /* 修改文字颜色，可根据需要调整颜色值 */
    border: 1px solid #ce5050;
}

.amis-scope .a-AsideNav-item {
    background-color: #FFFFFF;
    border-radius: 8px; /* 圆角矩形，可根据需要调整数值 */
    color: #1a1917; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-item:hover {
    background-color: #cfcfcf;
    color: #1a1917; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-itemLabel  {
    /* background-color: #cfcfcf; */
    color: #1a1917; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-item:hover > a >  .a-AsideNav-itemLabel {
    color: #FFFFFF;
}

.amis-scope .a-AsideNav-item.is-active > a {
    border-radius: 8px;
    background-color: #ce5050;
    color: #FFFFFF; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-item.is-active .a-AsideNav-item .a-AsideNav-itemLabel{
    color: #1a1917; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-item.is-open > .a-AsideNav-item:hover > .a-AsideNav-itemLabel{
    color: #FFFFFF; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-item:hover > a > .a-AsideNav-itemLabel {
    color: unset;
}

.amis-scope .a-AsideNav-item.is-active .a-AsideNav-itemLabel {
    color: #FFFFFF; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-item a {
    border-radius: 8px;
}


.a-AsideNav-item.is-open:has(.a-AsideNav-item.is-open.is-active) {
    background-color: transparent;
    color: #FFFFFF;
}


a-AsideNav-item.is-open > a > i {
    color: #FFFFFF;
}

a-AsideNav-item.is-open > a > a-AsideNav-itemLabel {
    color: #FFFFFF;
}

.amis-scope .a-AsideNav-item a:hover {
    background-color: #ce5050;
    border-radius: 8px;
    color: #FFFFFF; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-AsideNav-item.is-active > a:hover {
    background: #ce5050;
    border-radius: 8px;
    color: #FFFFFF; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-Layout-brand, .amis-scope .a-Layout-brandBar, .amis-scope .a-Layout-aside {
    background-color: #FFFFFF;
    box-shadow: #cfcfcf;

}

.amis-scope a {
    color: #ce5050; /* 修改文字颜色，可根据需要调整颜色值 */
}

.amis-scope .a-Switch.is-checked {
    background:  #ce5050;
}

.amis-scope .m-l-sm {
    color: #ce5050;
}

.amis-scope .a-AsideNav-subList {
    background: #FFFFFF;
    border-radius: 8px!important; /* 圆角矩形，可根据需要调整数值 */
}

.amis-scope .a-AsideNav-itemArrow svg {
    fill : #ce5050;
}

.amis-scope .a-AsideNav-itemArrow::after {
    color : #ce5050;
}

.amis-scope .a-AsideNav-itemArrow::before {
    color : #ce5050;
}

.amis-scope .a-AsideNav-itemArrow:empty {
    background-repeat: no-repeat;
    width: 0.625rem;
    height: 0.625rem;
    background-position: center center;
    display: inline-block;
    background: none !important; /* 移除原有背景 */
}

.amis-scope .a-AsideNav-item a:hover .a-AsideNav-itemArrow:empty:before {
    background: url("data:image/svg+xml,%3C%3Fxml version='1.0' encoding='UTF-8'%3F%3E%3Csvg viewBox='0 0 513 1021' version='1.1' xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'%3E%3Cg id='right-arrow' fill='%23ffffff' fill-rule='nonzero'%3E%3Cpath d='M56.559054,1013.77369 L512.908116,512.684524 L56.559054,12.234501 C49.4114678,2.93455912 37.6664238,-1.59188176 26.1262324,0.505948246 C14.586041,2.60377825 5.18544409,10.9741727 1.76815516,22.1946471 C-1.64913377,33.4151214 1.48980228,45.6045351 9.901516,53.778884 L424.706197,512.684524 L12.458094,969.672731 C2.45820596,982.551498 4.01297737,1000.9483 16.0324422,1011.96615 C28.0519071,1022.98399 46.5142346,1022.93619 58.476487,1011.85626 L56.559054,1013.77369 Z' id='è·¯å¾'%3E%3C/path%3E%3C/g%3E%3C/svg%3E%0A");
    background-repeat: no-repeat;
    width: 0.625rem;
    height: 0.625rem;
    background-position: center center;
    display: inline-block;
}

.amis-scope .a-AsideNav-itemArrow:empty:before {
    content: "";
    background: url("data:image/svg+xml,%3C%3Fxml version='1.0' encoding='UTF-8'%3F%3E%3Csvg viewBox='0 0 513 1021' version='1.1' xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'%3E%3Cg id='right-arrow' fill='%23ce5050' fill-rule='nonzero'%3E%3Cpath d='M56.559054,1013.77369 L512.908116,512.684524 L56.559054,12.234501 C49.4114678,2.93455912 37.6664238,-1.59188176 26.1262324,0.505948246 C14.586041,2.60377825 5.18544409,10.9741727 1.76815516,22.1946471 C-1.64913377,33.4151214 1.48980228,45.6045351 9.901516,53.778884 L424.706197,512.684524 L12.458094,969.672731 C2.45820596,982.551498 4.01297737,1000.9483 16.0324422,1011.96615 C28.0519071,1022.98399 46.5142346,1022.93619 58.476487,1011.85626 L56.559054,1013.77369 Z' id='è·¯å¾'%3E%3C/path%3E%3C/g%3E%3C/svg%3E%0A");
    background-repeat: no-repeat;
    width: 0.625rem;
    height: 0.625rem;
    background-position: center center;
    display: inline-block;
}


.amis-scope .a-AsideNav-item.is-open > a > .a-AsideNav-itemArrow:empty:before {
    background: url("data:image/svg+xml,%3C%3Fxml version='1.0' encoding='UTF-8'%3F%3E%3Csvg viewBox='0 0 513 1021' version='1.1' xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'%3E%3Cg id='right-arrow' fill='%23ffffff' fill-rule='nonzero'%3E%3Cpath d='M56.559054,1013.77369 L512.908116,512.684524 L56.559054,12.234501 C49.4114678,2.93455912 37.6664238,-1.59188176 26.1262324,0.505948246 C14.586041,2.60377825 5.18544409,10.9741727 1.76815516,22.1946471 C-1.64913377,33.4151214 1.48980228,45.6045351 9.901516,53.778884 L424.706197,512.684524 L12.458094,969.672731 C2.45820596,982.551498 4.01297737,1000.9483 16.0324422,1011.96615 C28.0519071,1022.98399 46.5142346,1022.93619 58.476487,1011.85626 L56.559054,1013.77369 Z' id='è·¯å¾'%3E%3C/path%3E%3C/g%3E%3C/svg%3E%0A");
    background-repeat: no-repeat;
    width: 0.625rem;
    height: 0.625rem;
    background-position: center center;
    display: inline-block;
}
"""  # noqa: E501

web_url = "https://docs.sayu-bot.com"
login_html = f"""
<html>
<head>
<style>
{style}
</style>
</head>
<p>
  <p align='center'>
      <a href='{web_url}'>
          <img src='/webstatic/ICON.png'
          width='256' height='256' alt='GenshinUID'>
      </a>
  </p>
  <h1 align='center'>GsCore 网页控制台</h1>
  <div align='center'>
      <a href='{web_url}/Started/InstallCore.html' target='_blank'>
      安装文档</a> &nbsp; · &nbsp;
      <a href='{web_url}/PluginsHelp/GenshinUID.html/'
      target='_blank'>指令列表</a> &nbsp; · &nbsp;
      <a href='{web_url}/FAQ/'>常见问题</a>
  </div>
  <h5 align='center'>
    {sup}
  </h5>
  <h5 align='center'>
    ✨支持平台: QQ群/频道 · Onebot v11/v12 · WeChat · 飞书 · Tg · Kook/dodo · Dc · 钉钉✨
  </h5>
</p>
</html>
"""

footer_html = f"""
<p align='right'>
    <div class='p-2 text-center bg-light'>Copyright © 2021 - 2024
        <a href='https://github.com/Genshin-bots/gsuid_core' target='_blank'
         class='link-secondary'>早柚核心 {gscore_version}
        </a>
         X
        <a target='_blank'
         href='https://github.com/amisadmin/fastapi_amis_admin/'
         class='link-secondary' rel='noopener'>
         fastapi_amis_admin {fastapi_amis_admin.__version__}
        </a>
    </div>
</p>
"""

gsuid_webconsole_help = """
## 初次使用

欢迎进入网页控制台!

Admin账户可以通过左侧的选项进入不同的数据库直接修改,**首次登陆的Admin账户别忘了修改你的密码!**

普通账户可以通过左侧的选项进行绑定CK或者SK

未来还会加入更多功能!

## 丨我该如何获取Cookies？[#92](https://github.com/KimigaiiWuyi/GenshinUID/issues/92)
（[@RemKeeper](https://github.com/RemKeeper)）

```js
var cookie = document.cookie;
var Str_Num = cookie.indexOf('_MHYUUID=');
cookie = cookie.substring(Str_Num);
var ask = confirm('Cookie:' + cookie + '按确认，然后粘贴至Cookies或者Login_ticket选框内');
if (ask == true) {
  copy(cookie);
  msg = cookie
} else {
  msg = 'Cancel'
}
```

1. 复制上面全部代码，然后打开[米游社BBS](https://bbs.mihoyo.com/ys/)
2. 在页面上右键检查或者Ctrl+Shift+i
3. 选择控制台（Console），粘贴，回车，在弹出的窗口点确认（点完自动复制）
4. 然后在和机器人的私聊窗口，粘贴发送即可

**警告：Cookies属于个人隐私，其效用相当于账号密码，请勿随意公开！**

## 丨获取米游社Stoken([AutoMihoyoBBS](https://github.com/Womsxd/AutoMihoyoBBS))

```js
var cookie = document.cookie;
var ask = confirm('Cookie:' + cookie + '按确认，然后粘贴至Cookies或者Login_ticket选框内');
if (ask == true) {
  copy(cookie);
  msg = cookie
} else {
  msg = 'Cancel'
}
```

1. 复制上面全部代码，然后打开[米游社账户登录界面](http://user.mihoyo.com/)
2. 在页面上右键检查或者Ctrl+Shift+i
3. 选择控制台（Console），粘贴，回车，在弹出的窗口点确认（点完自动复制）
4. 然后在和机器人的私聊窗口，粘贴发送即可

**警告：Cookies属于个人隐私，其效用相当于账号密码，请勿随意公开！**

## 获取CK通则

**如果获取到的Cookies字段不全，无法通过校验**
**推荐重新登陆米游社再进行获取**

## 网页端 #92 [@RemKeeper](https://github.com/RemKeeper)
[通过网页控制台简易获取Cookies](https://github.com/KimigaiiWuyi/GenshinUID/issues/92)
## 安卓 [@shirokurakana](https://github.com/shirokurakana)
[通过额外APP获取Cookies](https://github.com/KimigaiiWuyi/GenshinUID/issues/203)
## IOS [@741807012](https://github.com/741807012)
[通过快捷指令获取Cookies](https://github.com/KimigaiiWuyi/GenshinUID/issues/201)
"""
