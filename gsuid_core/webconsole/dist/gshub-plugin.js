/**
 * GsHub plugin page SDK.
 *
 * 源文件：gsuid_hub/public/gshub-plugin.js（Vite 原样拷进 dist，不参与 hash）。
 * Core 从 webconsole/dist（或 data/dist）读取，挂到 /plugin-pages/_sdk/gshub-plugin.js。
 *
 * Hub iframe 会带 ?locale=&theme=&style=&token= 。
 * 切主题时父页 postMessage `{ type: "gshub:theme", mode, style, vars }`，
 * 不必整页重载。同源时也可从 localStorage.auth_token 读取登录态。
 *
 * 用法：
 *   <script src="/plugin-pages/_sdk/gshub-plugin.js"></script>
 *   <script>
 *     GsHubPlugin.ready.then(() => {
 *       document.title = GsHubPlugin.t('title');
 *       GsHubPlugin.api('/api/myplugin/items').then(render);
 *     });
 *     GsHubPlugin.onTheme(function (theme) {
 *       // theme.mode === 'light' | 'dark'
 *     });
 *   </script>
 *
 * 页面 CSS 用 Hub token：
 *   background: hsl(var(--background));
 *   color: hsl(var(--foreground));
 *   border-color: hsl(var(--border));
 */
(function (global) {
  'use strict';

  var params = new URLSearchParams(global.location.search);
  var locale = params.get('locale') || 'zh-CN';
  var theme = params.get('theme') || 'light';
  var style = params.get('style') || 'solid';
  var token = params.get('token') || '';
  var iconColor = '';
  var themeListeners = [];

  if (!token) {
    try {
      token = global.localStorage.getItem('auth_token') || '';
    } catch (e) {
      token = '';
    }
  }

  var root = document.documentElement;

  function injectBaseCss() {
    if (document.getElementById('gshub-plugin-theme-css')) return;
    var el = document.createElement('style');
    el.id = 'gshub-plugin-theme-css';
    el.textContent =
      'html{color-scheme:light}' +
      'html.dark{color-scheme:dark}';
    var host = document.head || document.documentElement;
    host.appendChild(el);
  }

  function applyTheme(next) {
    if (next.mode === 'light' || next.mode === 'dark') {
      theme = next.mode;
    }
    if (typeof next.style === 'string' && next.style) {
      style = next.style;
    }
    if (typeof next.iconColor === 'string') {
      iconColor = next.iconColor;
    }
    root.classList.toggle('dark', theme === 'dark');
    root.setAttribute('data-theme', theme);
    root.setAttribute('data-style', style);
    if (iconColor) {
      root.setAttribute('data-icon-color', iconColor);
    }
    var vars = next.vars;
    if (vars && typeof vars === 'object') {
      Object.keys(vars).forEach(function (name) {
        if (name.indexOf('--') === 0 && typeof vars[name] === 'string') {
          root.style.setProperty(name, vars[name]);
        }
      });
    }
    if (global.GsHubPlugin) {
      global.GsHubPlugin.theme = theme;
      global.GsHubPlugin.style = style;
      global.GsHubPlugin.iconColor = iconColor;
      if (typeof next.color === 'string') {
        global.GsHubPlugin.color = next.color;
      }
    }
    var snapshot = {
      mode: theme,
      style: style,
      iconColor: iconColor,
      color: typeof next.color === 'string' ? next.color : '',
      vars: vars || {},
    };
    themeListeners.forEach(function (fn) {
      fn(snapshot);
    });
  }

  injectBaseCss();
  applyTheme({ mode: theme, style: style, vars: {} });
  root.setAttribute('lang', locale);

  var catalog = {};

  function lookup(obj, key) {
    var parts = String(key).split('.');
    var cur = obj;
    for (var i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== 'object' || !(parts[i] in cur)) {
        return null;
      }
      cur = cur[parts[i]];
    }
    return typeof cur === 'string' ? cur : null;
  }

  function interpolate(text, vars) {
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, function (_, name) {
      return name in vars ? String(vars[name]) : '{' + name + '}';
    });
  }

  function t(key, vars) {
    var hit = lookup(catalog, key);
    if (hit == null) return key;
    return interpolate(hit, vars);
  }

  function pageBase() {
    var path = global.location.pathname;
    if (!path.endsWith('/')) {
      var cut = path.lastIndexOf('/');
      path = cut >= 0 ? path.slice(0, cut + 1) : path + '/';
    }
    return path;
  }

  function loadLocales() {
    var url = pageBase() + 'locales/' + encodeURIComponent(locale) + '.json';
    return fetch(url, { credentials: 'same-origin' })
      .then(function (res) {
        if (!res.ok) return {};
        return res.json();
      })
      .then(function (data) {
        if (data && typeof data === 'object') {
          catalog = data;
        }
        return catalog;
      })
      .catch(function () {
        catalog = {};
        return catalog;
      });
  }

  function fetchApi(path, opts) {
    var options = opts ? Object.assign({}, opts) : {};
    var headers = Object.assign({}, options.headers || {});
    if (token && !headers.Authorization && !headers.authorization) {
      headers.Authorization = 'Bearer ' + token;
    }
    options.headers = headers;
    options.credentials = options.credentials || 'same-origin';
    return fetch(path, options);
  }

  function api(path, opts) {
    return fetchApi(path, opts).then(function (res) {
      return res.json().then(function (body) {
        if (!res.ok) {
          var detail = body && (body.msg || body.detail);
          throw new Error(detail || res.statusText || 'request failed');
        }
        if (body && typeof body.status === 'number' && body.status !== 0) {
          throw new Error(body.msg || 'request failed');
        }
        return body && 'data' in body ? body.data : body;
      });
    });
  }

  function blob(path, opts) {
    return fetchApi(path, opts).then(function (res) {
      if (!res.ok) {
        throw new Error('preview failed');
      }
      return res.blob();
    });
  }

  function onTheme(fn) {
    themeListeners.push(fn);
    return function () {
      themeListeners = themeListeners.filter(function (item) {
        return item !== fn;
      });
    };
  }

  function requestParentTheme() {
    try {
      if (global.parent && global.parent !== global) {
        global.parent.postMessage({ type: 'gshub:theme-request' }, '*');
      }
    } catch (e) {
      // 跨域或无父窗时忽略
    }
  }

  global.addEventListener('message', function (ev) {
    var data = ev.data;
    if (!data || data.type !== 'gshub:theme') return;
    applyTheme(data);
  });

  var ready = loadLocales().then(function (cat) {
    requestParentTheme();
    return cat;
  });

  global.GsHubPlugin = {
    locale: locale,
    theme: theme,
    style: style,
    iconColor: iconColor,
    token: token,
    t: t,
    ready: ready,
    api: api,
    blob: blob,
    fetch: fetchApi,
    onTheme: onTheme,
    applyTheme: applyTheme,
    catalog: function () {
      return catalog;
    },
  };
})(typeof window !== 'undefined' ? window : this);
