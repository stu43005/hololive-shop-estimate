# Dify Kubernetes 部署指南

本目錄包含將 Dify 部署到 Kubernetes 的所有 manifest 檔案。

## 目錄結構

```
dify-deploy/
├── 00-namespace.yaml      # Namespace 定義
├── 01-configmap.yaml      # 共用配置 (dify-shared-config)
├── 02-secrets.yaml        # 敏感資料 (密碼、API Keys)
├── 03-pvc.yaml            # PersistentVolumeClaims
├── 10-postgres.yaml       # PostgreSQL 資料庫
├── 11-redis.yaml          # Redis 快取
├── 12-weaviate.yaml       # Weaviate 向量資料庫
├── 13-sandbox.yaml        # Dify Sandbox (程式碼執行)
├── 14-ssrf-proxy.yaml     # SSRF Proxy (Squid)
├── 20-api.yaml            # Dify API 服務
├── 21-worker.yaml         # Celery Worker
├── 22-worker-beat.yaml    # Celery Beat 排程器
├── 23-web.yaml            # Web 前端
├── 24-plugin-daemon.yaml  # Plugin Daemon
├── 30-nginx.yaml          # Nginx 反向代理
├── kustomization.yaml     # Kustomize 配置
└── README.md              # 本文件
```

## 前置需求

- Kubernetes 叢集 (v1.24+)
- kubectl 已配置
- (選用) kustomize

## 快速部署

### 使用 kubectl

```bash
# 部署所有資源
kubectl apply -f dify-deploy/

# 或依序部署
kubectl apply -f dify-deploy/00-namespace.yaml
kubectl apply -f dify-deploy/01-configmap.yaml
kubectl apply -f dify-deploy/02-secrets.yaml
kubectl apply -f dify-deploy/03-pvc.yaml
kubectl apply -f dify-deploy/10-postgres.yaml
kubectl apply -f dify-deploy/11-redis.yaml
kubectl apply -f dify-deploy/12-weaviate.yaml
kubectl apply -f dify-deploy/13-sandbox.yaml
kubectl apply -f dify-deploy/14-ssrf-proxy.yaml
kubectl apply -f dify-deploy/20-api.yaml
kubectl apply -f dify-deploy/21-worker.yaml
kubectl apply -f dify-deploy/22-worker-beat.yaml
kubectl apply -f dify-deploy/23-web.yaml
kubectl apply -f dify-deploy/24-plugin-daemon.yaml
kubectl apply -f dify-deploy/30-nginx.yaml
```

### 使用 Kustomize

```bash
kubectl apply -k dify-deploy/
```

## 配置說明

### ConfigMap (dify-shared-config)

所有共用的環境變數都儲存在 `01-configmap.yaml` 中的 `dify-shared-config`
ConfigMap。這包括：

- 資料庫連線設定
- Redis 設定
- 向量資料庫設定
- 日誌設定
- 工作流程設定
- 其他應用程式設定

### Secrets (dify-secrets)

敏感資料儲存在 `02-secrets.yaml` 中的 `dify-secrets` Secret：

- `SECRET_KEY` - 應用程式加密金鑰
- `DB_PASSWORD` - 資料庫密碼
- `REDIS_PASSWORD` - Redis 密碼
- `WEAVIATE_API_KEY` - Weaviate API 金鑰
- `SANDBOX_API_KEY` - Sandbox API 金鑰
- `PLUGIN_DAEMON_KEY` - Plugin Daemon 金鑰
- `PLUGIN_DIFY_INNER_API_KEY` - 內部 API 金鑰

**重要**: 部署前請務必修改這些預設密碼！

## 元件說明

| 元件          | 描述              | Port        |
| ------------- | ----------------- | ----------- |
| postgres      | PostgreSQL 資料庫 | 5432        |
| redis         | Redis 快取        | 6379        |
| weaviate      | 向量資料庫        | 8080, 50051 |
| sandbox       | 程式碼執行沙箱    | 8194        |
| ssrf-proxy    | SSRF 代理         | 3128        |
| api           | Dify API 服務     | 5001        |
| worker        | Celery Worker     | -           |
| worker-beat   | Celery Beat       | -           |
| web           | Web 前端          | 3000        |
| plugin-daemon | 插件服務          | 5002, 5003  |
| nginx         | 反向代理          | 80          |

## 存取服務

部署完成後，可以透過以下方式存取：

```bash
# 取得 Nginx LoadBalancer IP
kubectl get svc nginx -n dify

# 或使用 port-forward
kubectl port-forward svc/nginx 8080:80 -n dify
```

然後開啟瀏覽器存取 `http://localhost:8080`

## 監控與除錯

```bash
# 查看所有 Pod 狀態
kubectl get pods -n dify

# 查看 Pod 日誌
kubectl logs -f deployment/api -n dify
kubectl logs -f deployment/worker -n dify

# 進入 Pod
kubectl exec -it deployment/api -n dify -- /bin/bash
```

## 自訂配置

### 修改資源限制

編輯各 Deployment 檔案中的 `resources` 區塊：

```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "200m"
  limits:
    memory: "2Gi"
    cpu: "1000m"
```

### 修改 PVC 大小

編輯 `03-pvc.yaml` 中的 `storage` 值：

```yaml
resources:
  requests:
    storage: 20Gi # 修改為所需大小
```

### 使用 Ingress 替代 LoadBalancer

建立 Ingress 資源：

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: dify-ingress
  namespace: dify
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
spec:
  ingressClassName: nginx
  rules:
    - host: dify.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: nginx
                port:
                  number: 80
```

## 清理

```bash
# 刪除所有資源
kubectl delete -k dify-deploy/

# 或
kubectl delete namespace dify
```

## 注意事項

1. **生產環境**: 請務必修改所有預設密碼和金鑰
2. **持久化儲存**: 確保 PVC 使用適當的 StorageClass
3. **資源配置**: 根據實際需求調整 CPU 和記憶體限制
4. **網路安全**: 考慮使用 NetworkPolicy 限制 Pod 間通訊
5. **備份**: 定期備份 PostgreSQL 和 Redis 資料
