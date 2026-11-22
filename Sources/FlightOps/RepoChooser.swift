import SwiftUI

struct RepoChooser: View {
    @ObservedObject var m: Monitor
    var done: () -> Void
    @State private var picked: Set<String> = []
    @State private var search = ""
    @State private var saving = false

    private var shown: [AvailableRepo] {
        let query = search.trimmingCharacters(in: .whitespaces).lowercased()
        return m.available.filter { query.isEmpty || $0.name.lowercased().contains(query) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Choose the repos to follow").font(.title3.weight(.semibold))
            Text("FlightOps reads them through your GitHub CLI login. Nothing is sent anywhere else, and it never writes to GitHub.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextField("Search", text: $search).textFieldStyle(.roundedBorder)
            Group {
                if m.loadingAvailable {
                    ProgressView("Asking GitHub…").frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let error = m.availableError, m.available.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "exclamationmark.triangle").font(.title2).foregroundStyle(Theme.caution)
                        Text(error).multilineTextAlignment(.center).font(.callout).textSelection(.enabled)
                        Button("Try Again") { Task { await m.loadAvailable() } }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List(shown) { repo in
                        Toggle(isOn: binding(repo.name)) {
                            HStack {
                                Text(repo.name)
                                if repo.isPrivate { Image(systemName: "lock.fill").font(.caption2).foregroundStyle(.secondary) }
                                Spacer()
                                Text(repo.pushedAt.map { Clock.age($0) } ?? "").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.checkbox)
                    }
                    .listStyle(.inset)
                }
            }
            .frame(maxHeight: .infinity)
            HStack {
                Text(picked.isEmpty ? "None picked" : "\(picked.count) picked").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Cancel", action: done).keyboardShortcut(.cancelAction)
                Button(saving ? "Saving…" : "Follow These Repos") {
                    saving = true
                    let listed: [String] = m.available.map(\.name).filter { picked.contains($0) }
                    let others: [String] = picked.subtracting(listed).sorted()
                    let names = listed + others
                    Task {
                        await m.use(names)
                        saving = false
                        done()
                    }
                }
                .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(picked.isEmpty || saving)
            }
        }
        .padding(.horizontal, Theme.margin).padding(.bottom, 12)
        .task {
            picked = Set(m.currentRepos)
            if m.available.isEmpty { await m.loadAvailable() }
        }
    }

    private func binding(_ name: String) -> Binding<Bool> {
        Binding(get: { picked.contains(name) }, set: { on in if on { picked.insert(name) } else { picked.remove(name) } })
    }
}
