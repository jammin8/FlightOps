import ServiceManagement
import SwiftUI



struct SettingsView: View {
    @ObservedObject var m: Monitor
    @State private var openAtLogin = SMAppService.mainApp.status == .enabled
    @State private var loginError: String?

    var body: some View {
        TabView {
            Form {
                Section {
                    Toggle("Open FlightOps at login", isOn: $openAtLogin)
                        .onChange(of: openAtLogin) { _, on in
                            do {
                                try on ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister()
                                loginError = nil
                            } catch {
                                openAtLogin = SMAppService.mainApp.status == .enabled
                                loginError = "macOS didn't allow that change. Check System Settings › General › Login Items."
                            }
                        }
                    if let loginError { Text(loginError).font(.callout).foregroundStyle(.secondary) }
                }
                Section("Alerts") {
                    LabeledContent("Dismissed warnings") {
                        Button("Show \(m.dismissed.count) again") { m.restoreDismissed() }.disabled(m.dismissed.isEmpty)
                    }
                }
            }
            .formStyle(.grouped)
            .tabItem { Label("General", systemImage: "gearshape") }

            Form {
                Section {
                    LabeledContent("History") {
                        Text("Analysis timelines and bottleneck samples are kept for 7 days. Each PR's story is kept for a year, the raw readings log for 90 days, and the GitHub receipts and facts caches until replaced; all on this Mac.").foregroundStyle(.secondary)
                    }
                    LabeledContent("Board animations") {
                        Button("Preview") { NotificationCenter.default.post(name: .previewBoardMotion, object: nil) }
                    }
                    Text("Animations follow System Settings › Accessibility › Display › Reduce motion.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
            .tabItem { Label("Data", systemImage: "chart.bar") }
        }
        .frame(width: 460, height: 300)
        .onAppear { openAtLogin = SMAppService.mainApp.status == .enabled }
    }
}
