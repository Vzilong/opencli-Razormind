# 本地模型连接

平台的“模型与连接”支持 Ollama、LM Studio、vLLM 和 LocalAI 的 OpenAI 兼容服务。
模型运行在用户管理的机器上；平台负责连接、采集和分析编排、引用保存以及 API/MCP 查询。

1. 在模型服务中下载并加载模型，启动 HTTP 服务。
2. 打开 `/providers`，选择对应本地服务，填写从**平台后端**可访问的 API 地址。
3. 服务未开启鉴权时，访问密钥可留空；开启鉴权时填写该服务的密钥。
4. 获取模型列表，选择模型并保存供应商，然后点击“设为默认”。仅添加供应商不会设置研究所用的 `chat` 默认模型。
5. 在首发研究页检查分析能力，运行带公开来源的问题。模型无法产生有效引用时，结果仍保持 `partial` 并报告缺口。

| 服务 | 后端与模型在同一台主机原生运行时的地址 |
| --- | --- |
| Ollama | `http://127.0.0.1:11434/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| vLLM | `http://127.0.0.1:8000/v1` |
| LocalAI | `http://127.0.0.1:8080/v1` |

地址和端口以模型服务实际配置为准，必须包含其 OpenAI 兼容 API 前缀。
参考：[Ollama 官方兼容说明](https://docs.ollama.com/api/openai-compatibility)、
[LM Studio 官方兼容说明](https://lmstudio.ai/docs/developer/openai-compat)。

后端在 Docker 中时，`127.0.0.1` 指向后端容器。宿主机模型应使用
`http://host.docker.internal:模型端口/v1`；Linux Docker 需为后端服务添加：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

模型服务还需监听可从容器访问的接口。局域网部署使用模型机器的地址，并按自己的网络边界配置鉴权。
平台的本地类型允许私有地址，仍保留 URL 校验和连接 IP 固定；普通云供应商不获得这项豁免。
未填写本地地址或模型时会明确报错，不会回退到云服务或云模型。

本地类型通过同一研究引擎产生结果，下游 Codex/Claude Code 的 API/MCP 接口不变。
兼容接口并不代表所有本地模型都能可靠执行工具或生成合格引用，需以实际研究结果验收。
