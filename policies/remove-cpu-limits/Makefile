# Builds, tests and annotates the policy.
#   make test                   unit tests (Go)
#   make policy.wasm            build with TinyGo (or `make docker-build` with the TinyGo image)
#   make annotated-policy.wasm  add metadata.yml and README.md (kwctl)
#   make e2e-tests              run the policy with kwctl against test_data/ (bats)
TINYGO ?= tinygo
TINYGO_VERSION ?= 0.39.0
SOURCE_FILES := $(shell find . -name '*.go' -not -name '*_test.go')

.PHONY: build docker-build test e2e-tests clean

build: policy.wasm

go.sum: go.mod
	go mod tidy

policy.wasm: go.sum $(SOURCE_FILES)
	GOFLAGS="-buildvcs=false" $(TINYGO) build -o policy.wasm -target=wasi -no-debug .

docker-build: go.sum
	docker run --rm -e GOFLAGS="-buildvcs=false" -v "$(CURDIR)":/src -w /src tinygo/tinygo:$(TINYGO_VERSION) \
		tinygo build -o policy.wasm -target=wasi -no-debug .

annotated-policy.wasm: policy.wasm metadata.yml README.md
	kwctl annotate -m metadata.yml -u README.md -o annotated-policy.wasm policy.wasm

test: go.sum
	go test -v ./...

e2e-tests: annotated-policy.wasm
	bats e2e.bats

clean:
	rm -f policy.wasm annotated-policy.wasm
