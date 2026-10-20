module github.com/cdalar/cluster-resource-allocation/policies/remove-cpu-limits

go 1.25

require (
	github.com/kubewarden/policy-sdk-go v0.13.1
	github.com/wapc/wapc-guest-tinygo v0.3.3
)

require (
	github.com/go-openapi/strfmt v0.21.3 // indirect
	github.com/kubewarden/k8s-objects v1.29.0-kw1 // indirect
)

// go-openapi/strfmt pulls in packages TinyGo cannot compile (net lookups); Kubewarden maintains a stripped fork.
replace github.com/go-openapi/strfmt => github.com/kubewarden/strfmt v0.1.3
