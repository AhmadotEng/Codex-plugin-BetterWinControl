MinHook 1.3.4 is pinned to commit c3fcafdc10146beb5919319d0683e44e3c30d537.
Upstream: https://github.com/TsudaKageyu/minhook/tree/v1.3.4
Archive SHA-256: 172708123daa0c98d20d3a980b16a50be14af243dc95dee6f79c24193ad010e4
Its BSD-2-Clause notice and bundled HDE notices are retained under vendor/minhook-1.3.4.

nlohmann/json 3.11.3 is used only by the host JSON parser.
Upstream: https://github.com/nlohmann/json/tree/v3.11.3
Single header SHA-256: 9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6
Its MIT notice is retained in vendor/json/LICENSE.MIT and its header.

No ProtoInput binary or source is bundled. The implementation uses its general
audited technique of in-process API detours, with a separate synthetic-input
protocol and dispatch-only virtual state. It does not capture physical devices.
