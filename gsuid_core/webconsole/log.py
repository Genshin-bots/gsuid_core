HIGHLIGHT = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0'


def render_html():
    return {
        "type": "page",
        "title": "",
        "css": f"{HIGHLIGHT}/styles/atom-one-dark.min.css",
        "js": [
            f"{HIGHLIGHT}/highlight.min.js",
            f"{HIGHLIGHT}/languages/json.min.js",
        ],
        "body": [
            {
                "type": "custom",
                "id": "log_viewer",
                "html": HTML,
                "onMount": ON_MOUNT_SSE,
                "onUnmount": ON_UNMOUNT_SSE,
            }
        ],
    }


HTML = """
<style>
    :root {
        --theme-color: #ce5050;
        --background-color: #1e1e1e;
        --text-color: #d4d4d4;
        --log-bg-color: #252526;
        --border-color: #444;
    }

    #log-container-inner {
        height: 75vh; /* 可以适当增加高度 */
        background-color: var(--log-bg-color);
        border: 1px solid var(--border-color);
        border-radius: 8px;
        padding: 8px 12px; /* 大幅减小内边距 */
        overflow-y: auto;
        font-family: 'Consolas', 'Monaco', 'Menlo', monospace;
        /* 减小字体和行高以容纳更多行 */
        font-size: 13px;
        line-height: 1.4;
    }

    /* --- 核心改动：日志条目的新样式 --- */
    .log-entry {
        padding: 3px 0; /* 减小每个条目的垂直内边距 */
        margin: 0;
        border-bottom: 1px solid var(--border-color);
        white-space: normal; /* 允许自动换行 */
    }
    .log-entry:last-child {
        border-bottom: none;
    }

    /* 将元数据（时间、级别）变为行内元素 */
    .log-time {
        margin-right: 12px;
        color: #888;
        display: inline; /* 变为行内元素 */
    }
    .log-level {
        font-weight: bold;
        padding: 1px 5px; /* 减小内边距 */
        border-radius: 3px;
        color: #fff;
        margin-right: 10px;
        display: inline-block; /* 允许设置padding */
        font-size: 12px; /* 可以比正文小一点 */
    }
    .log-level.INFO { background-color: #2196F3; }
    .log-level.SUCCESS { background-color: #43A047; }
    .log-level.DEBUG { background-color: #F39333; }
    .log-level.WARN { background-color: #FFC107; color: #333; }
    .log-level.WARNING { background-color: #FFC107; color: #333; }
    .log-level.ERROR { background-color: #F44336; }
    .log-level.CRITICAL { background-color: #F44336; }
    .log-level.EXCEPTION { background-color: #F44336; }

    /* 日志内容样式 */
    .log-content {
        display: inline; /* 核心：让内容跟在级别后面 */
        word-break: break-all; /* 强制长单词换行 */
    }
    /* 对于普通文本的code标签 */
    .log-content > code {
        white-space: pre-wrap; /* 允许普通文本内容自动换行 */
    }
    /* 对于JSON的pre标签，它会自然成为块级元素，单独占行 */
    .log-content > pre {
        margin: 4px 0 0 0;
        padding: 5px 8px;
        background-color: rgba(0,0,0,0.2);
        border-radius: 4px;
    }
    .log-content > pre > code.hljs {
        padding: 0;
        background: transparent;
    }

    /* 关键词染色样式，保持不变 */
    .log-keyword-error { color: #ff8a80; font-weight: bold; }
    .log-keyword-success { color: #b9f6ca; font-weight: bold; }
    .log-keyword-blue { color: #407dff; font-weight: bold; }
    .log-keyword-id { color: #82aaff; background-color: #333a4f; padding: 1px 4px; border-radius: 3px; }
    .log-keyword-purple {
        color: #9c27b0; /* 深紫色，可根据需要调整色值 */
        font-weight: bold;
    }

    /* --- 页面头部样式，可以稍微紧凑一点 --- */
    .log-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid var(--theme-color); padding-bottom: 8px; margin-bottom: 12px; }
    .log-header h1 { color: var(--theme-color); margin: 0; font-size: 20px; font-family: 'Microsoft YaHei', sans-serif; }
    .log-status { font-size: 14px; font-family: Arial, sans-serif;}
    .log-status-indicator { width: 10px; height: 10px; margin-right: 6px; }
</style>
<div class="log-header">
    <h1>实时日志</h1>
    <div class="log-status">
        <span id="status-indicator-inner" class="log-status-indicator disconnected"></span>
        <span id="status-text-inner">未连接</span>
    </div>
</div>
<div id="log-container-inner"></div>
"""  # noqa: E501

ON_UNMOUNT_SSE = """
// 关闭 EventSource 连接
if (window.logEventSource) {
    window.logEventSource.close();
}
// 清理模拟服务器的定时器
if (window.mockServerInterval) {
    clearInterval(window.mockServerInterval);
}
"""

ON_MOUNT_SSE = """
    const logContainer = dom.querySelector('#log-container-inner');
    const statusIndicator = dom.querySelector('#status-indicator-inner');
    const statusText = dom.querySelector('#status-text-inner');

    const SSE_URL = '/corelogs';
    window.logEventSource = null;

    function appendLog(data) {
        const { timestamp, level, message, message_type } = data; // 从data中解构出新字段 message_type
        const logEntry = document.createElement('div');
        logEntry.className = 'log-entry';
        const time = new Date(timestamp).toLocaleTimeString();

        let messageHtml;
        let isJson = false;

        try {
            JSON.parse(message);
            isJson = true;
        } catch (e) {
            // isJson 保持 false
        }

        function highlightBrackets(text) {
            // 匹配[]内的内容（非贪婪模式），替换为带紫色样式的span
            return text.replace(/\[([^\]]+)\]/g, '<span class="log-keyword-purple">[$1]</span>');
        }

        // ======================= 核心修改点在这里 =======================
        if (isJson) {
            // 1. 如果是JSON，优先高亮显示 (逻辑不变)
            const jsonObj = JSON.parse(message);
            const formattedJson = JSON.stringify(jsonObj, null, 2);
            messageHtml = `<pre><code class="language-json hljs">${hljs.highlight(formattedJson, {language: 'json'}).value}</code></pre>`;

        } else if (message_type === 'html') {
            // 2. 如果后端标记为HTML，我们信任它，直接使用 message
            //    不再进行HTML转义，这样<span>标签就能生效
            const processedHtml = highlightBrackets(message);
            messageHtml = `<code>${processedHtml}</code>`;

        } else {
            // 3. 否则，就是未知来源的纯文本，为了安全必须进行转义
            const safeMessage = message.replace(/</g, "&lt;").replace(/>/g, "&gt;");
            const processedText = highlightBrackets(message);
            messageHtml = `<code>${processedText}</code>`;
        }
        // ======================= 修改结束 =======================

        logEntry.innerHTML = `
            <span class="log-time">${time}</span>
            <span class="log-level ${level}">${level}</span>
            <span class="log-content">${messageHtml}</span>
        `;

        logContainer.appendChild(logEntry);

        const isScrolledToBottom = logContainer.scrollHeight - logContainer.clientHeight <= logContainer.scrollTop + 5;
        if (isScrolledToBottom) {
            logContainer.scrollTop = logContainer.scrollHeight;
        }
    }

    function connectSSE() {
        // ... 此函数内容保持不变
        console.log(`尝试连接到 SSE 端点: ${SSE_URL}`);
        const evtSource = new EventSource(SSE_URL);
        window.logEventSource = evtSource;
        evtSource.onopen = () => {
            console.log('SSE 连接已建立。');
            statusIndicator.className = 'log-status-indicator connected';
            statusText.textContent = '已连接';
        };
        evtSource.onmessage = (event) => {
            try {
                const logData = JSON.parse(event.data);
                appendLog(logData);
            } catch (e) {
                console.error("解析SSE数据失败:", e);
                appendLog({ level: 'INFO', message: event.data, timestamp: new Date().toISOString() });
            }
        };
        evtSource.onerror = (err) => {
            console.error("EventSource 发生错误: ", err);
            statusIndicator.className = 'log-status-indicator disconnected';
            statusText.textContent = '连接中断，自动重连中...';
        };
    }

    connectSSE();
"""  # noqa: E501, W605
