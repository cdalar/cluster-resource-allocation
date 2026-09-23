# ADR-0001: Quota enforcement via Rancher Project quotas, managed as code

- **Status**: Proposed
- **Date**: 2026-09-23
- **Deciders**: Platform team

## Context

Projects/streams are already modelled as Rancher Projects containing several namespaces. We need hard caps per
project per cluster, with the ability for projects to distribute capacity across their own namespaces.

## Options

1. **Rancher Project resource quotas** (+ container default limits) — native, project-level cap with namespace
   split, visible in Rancher UI. Only works on Rancher-managed clusters.
2. **Plain `ResourceQuota` per namespace** via GitOps — works everywhere, but no project-level total; every
   namespace split needs a platform change.
3. **Capsule / HNC** (tenant-level quotas) — adds a tool that overlaps with Rancher Projects.

For applying allocations: Terraform `rancher2` provider, Fleet-managed `Project` CRs, or custom scripts.

## Decision

Use **Rancher Project quotas and container default limits**, driven from allocation files in Git and applied with
the **Terraform `rancher2` provider**. For clusters not managed by Rancher, generate equivalent `ResourceQuota`
and `LimitRange` manifests from the same files.

## Consequences

- One mental model (Rancher Project = unit of allocation) for projects and platform team.
- Project owners get self-service within their envelope.
- Manual quota edits in the Rancher UI must be restricted; drift detected by Terraform plan.
- Dependency on Rancher's quota implementation and its behaviour on upgrades.
