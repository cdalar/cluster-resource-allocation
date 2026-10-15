# Rancher Project names in Kibana (OpenTelemetry)

Downstream metrics carry the project ID (`p-xxxxx`); only the Rancher local cluster knows the project name. Ship the
Projects as a small lookup index and join it in Kibana (ES|QL `LOOKUP JOIN`, Elastic 8.18+ / 9.x).

> The collector config has not been run against a live stack. Check the first indexed documents (step 2).

## 1. Create the lookup index

```
PUT rancher-projects
{
  "settings": { "index.mode": "lookup" },
  "mappings": { "properties": {
    "project_id":   { "type": "keyword" },
    "project_name": { "type": "keyword" },
    "cluster_id":   { "type": "keyword" }
  } }
}
```

## 2. Local cluster: collector config from the chart

```bash
helm upgrade --install resource-report <chart> -n resource-report --reuse-values \
  --set otel.projects.enabled=true \
  --set otel.projects.elasticsearch.endpoint=https://elasticsearch.example.com:9200 \
  --set otel.projects.serviceAccount=<service account of your collector>
```

This renders the ConfigMap `<fullname>-otel-projects` (key `collector.yaml`) and, with `serviceAccount`, read access on
Projects. Use the file as an extra config of your collector (`--config=/conf/collector.yaml` next to your own), and
give it the API key in the environment variable `ELASTICSEARCH_API_KEY`.

The project ID is the document ID, so every pull overwrites the previous document and the join never returns
duplicate rows. Check one document: `GET rancher-projects/_search?size=1` must show `project_id`, `project_name`,
`cluster_id` as top-level fields.

## 3. Downstream clusters: add the project ID to the usage data

In the collector of each downstream cluster:

```yaml
processors:
  k8sattributes:
    extract:
      labels:
        - { tag_name: project_id, key: field.cattle.io/projectId, from: namespace }
```

The collector needs `get`/`list`/`watch` on namespaces (and pods, for `k8sattributes`).

## 4. Query in Kibana

```esql
FROM metrics-*
| LOOKUP JOIN rancher-projects ON project_id
| EVAL project_name = COALESCE(project_name, "unassigned")
| STATS cpu_req = SUM(k8s.container.cpu_request) BY cluster, project_name
```

Namespaces outside a Rancher Project (and System namespaces without the label) have no `project_name`, hence the
`COALESCE`.
