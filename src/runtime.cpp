#include "runtime.hpp"
#include "core.hpp"
#include <atomic>
#include <iostream>
#include <mutex>
#ifdef _WIN32
#include <windows.h>
#endif

namespace runtime {
namespace {
std::atomic<bool> stopped{false};
std::mutex callback_mutex;
std::function<void()> stop_callback;
#ifdef _WIN32
SERVICE_STATUS_HANDLE status_handle = nullptr;
SERVICE_STATUS status{};
std::function<int()> application;
int service_result = 1;
// Services have no console. Redirect C++ streams through ONE file handle;
// reopening stdout/stderr separately can fail under the Windows CRT sharing rules.
class LogBuffer final : public std::streambuf {
    std::ofstream file_;
    std::mutex mutex_;
protected:
    std::streamsize xsputn(const char* data, std::streamsize count) override {
        std::lock_guard<std::mutex> guard(mutex_);
        file_.write(data, count);
        file_.flush();
        return file_ ? count : 0;
    }
    int_type overflow(int_type value) override {
        if (traits_type::eq_int_type(value, traits_type::eof())) return traits_type::not_eof(value);
        auto c = traits_type::to_char_type(value);
        return xsputn(&c, 1) == 1 ? value : traits_type::eof();
    }
    int sync() override {
        std::lock_guard<std::mutex> guard(mutex_);
        file_.flush();
        return file_ ? 0 : -1;
    }
public:
    explicit LogBuffer(const fs::path& path) : file_(path, std::ios::app | std::ios::binary) {
        if (!file_) throw std::runtime_error("Cannot open service log");
    }
};
struct LogRedirect {
    LogBuffer buffer;
    std::streambuf* out;
    std::streambuf* error;
    explicit LogRedirect(const fs::path& path) : buffer(path), out(std::cout.rdbuf(&buffer)), error(std::cerr.rdbuf(&buffer)) {}
    ~LogRedirect() { std::cout.flush(); std::cerr.flush(); std::cout.rdbuf(out); std::cerr.rdbuf(error); }
};
std::unique_ptr<LogRedirect> log_redirect;
void report(DWORD state, DWORD error = NO_ERROR) {
    status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    status.dwCurrentState = state;
    status.dwControlsAccepted = state == SERVICE_RUNNING ? SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN : 0;
    status.dwWin32ExitCode = error;
    status.dwServiceSpecificExitCode = error ? static_cast<DWORD>(service_result) : 0;
    status.dwWaitHint = (state == SERVICE_START_PENDING || state == SERVICE_STOP_PENDING) ? 30000 : 0;
    status.dwCheckPoint = status.dwWaitHint ? 1 : 0;
    SetServiceStatus(status_handle, &status);
}
void WINAPI control(DWORD code) {
    if (code != SERVICE_CONTROL_STOP && code != SERVICE_CONTROL_SHUTDOWN) return;
    report(SERVICE_STOP_PENDING);
    stopped.store(true);
    std::lock_guard<std::mutex> guard(callback_mutex);
    if (stop_callback) stop_callback();
}
void WINAPI service_main(DWORD, LPWSTR*) {
    status_handle = RegisterServiceCtrlHandlerW(L"Toolstock", control);
    if (!status_handle) return;
    report(SERVICE_START_PENDING);
    service_result = application();
    report(SERVICE_STOPPED, service_result ? ERROR_SERVICE_SPECIFIC_ERROR : NO_ERROR);
}
#endif
void set(const std::string& key, const std::string& value) {
#ifdef _WIN32
    if (_putenv_s(key.c_str(), value.c_str()) != 0) throw std::runtime_error("Cannot set configuration field: " + key);
#else
    if (setenv(key.c_str(), value.c_str(), 1) != 0) throw std::runtime_error("Cannot set configuration field: " + key);
#endif
}
std::string path_text(const fs::path& p) {
    auto s = p.u8string();
    return {reinterpret_cast<const char*>(s.data()), s.size()};
}
fs::path executable(const char* arg) {
#ifdef _WIN32
    std::wstring path(32768, L'\0');
    auto n = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
    if (!n || n >= path.size()) throw std::runtime_error("Cannot locate executable");
    path.resize(n);
    return fs::path(path);
#else
    std::error_code ec;
    auto p = fs::read_symlink("/proc/self/exe", ec);
    return ec ? fs::absolute(arg) : p;
#endif
}
}

Options configure(int argc, char** argv) {
    Options options;
    fs::path config;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--config") {
            if (++i == argc || !config.empty()) throw std::runtime_error("Use --config FILE exactly once");
            config = fs::absolute(fs::u8path(argv[i]));
        } else if (arg == "--service") options.service = true;
        else options.args.push_back(arg);
    }
    const auto root = executable(argv[0]).parent_path();
    if (config.empty() && fs::is_regular_file(root / "toolstock.json")) config = root / "toolstock.json";
    if (!config.empty()) {
        json doc;
        try { doc = json::parse(readfile(config)); }
        catch (...) { throw std::runtime_error("Cannot read valid JSON configuration"); }
        if (!doc.is_object()) throw std::runtime_error("Configuration must be a JSON object");
        const std::set<std::string> allowed = {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGPASSFILE", "PGSSLMODE", "PGSSLROOTCERT", "PGCONNECT_TIMEOUT", "DATABASE_URL", "TOOLSTOCK_HOST", "TOOLSTOCK_PORT", "TOOLSTOCK_DATA", "TOOLSTOCK_WEB", "TOOLSTOCK_SQL", "TOOLSTOCK_DOCS", "TOOLSTOCK_LOG", "TOOLSTOCK_SECURE_COOKIE"};
        for (auto& [key, value] : doc.items()) {
            if (!allowed.contains(key) || !value.is_string()) throw std::runtime_error("Invalid configuration field: " + key);
            auto text = value.get<std::string>();
            if (text.find('\0') != std::string::npos) throw std::runtime_error("Invalid configuration value: " + key);
            if ((key == "PGPASSFILE" || key == "PGSSLROOTCERT" || key == "TOOLSTOCK_DATA" || key == "TOOLSTOCK_WEB" || key == "TOOLSTOCK_SQL" || key == "TOOLSTOCK_DOCS" || key == "TOOLSTOCK_LOG") && !text.empty()) {
                auto p = fs::u8path(text);
                text = path_text(p.is_absolute() ? p : config.parent_path() / p);
            }
            set(key, text);
        }
    }
    // Preserve existing environment-only Docker/developer deployments.
    for (auto [key, folder] : {std::pair{"TOOLSTOCK_WEB", "web"}, {"TOOLSTOCK_SQL", "sql"}, {"TOOLSTOCK_DOCS", "docs"}}) {
        if (env(key).empty()) set(key, path_text(fs::is_directory(root / folder) ? root / folder : fs::absolute(folder)));
    }
    if (env("PGCONNECT_TIMEOUT").empty()) set("PGCONNECT_TIMEOUT", "10");
    auto port = env("TOOLSTOCK_PORT", "8000");
    if (port.empty() || port.find_first_not_of("0123456789") != std::string::npos || port.size() > 5 || std::stoi(port) < 1 || std::stoi(port) > 65535) throw std::runtime_error("Invalid TOOLSTOCK_PORT");
    if (options.service) {
        auto log = env("TOOLSTOCK_LOG");
        if (log.empty()) throw std::runtime_error("Service requires TOOLSTOCK_LOG");
        auto path = fs::u8path(log);
        fs::create_directories(path.parent_path());
#ifdef _WIN32
        log_redirect = std::make_unique<LogRedirect>(path);
#endif
    }
    return options;
}
void on_stop(std::function<void()> callback) {
    std::lock_guard<std::mutex> guard(callback_mutex);
    stop_callback = std::move(callback);
    if (stopped.load() && stop_callback) stop_callback();
}
bool stopping() { return stopped.load(); }
void ready() {
#ifdef _WIN32
    if (status_handle) report(SERVICE_RUNNING);
#endif
}
int run(bool service, const std::function<int()>& app) {
    if (!service) return app();
#ifdef _WIN32
    application = app;
    SERVICE_TABLE_ENTRYW table[] = {{const_cast<LPWSTR>(L"Toolstock"), service_main}, {nullptr, nullptr}};
    if (!StartServiceCtrlDispatcherW(table)) {
        std::cerr << "StartServiceCtrlDispatcher failed: " << GetLastError() << std::endl;
        return 1;
    }
    return service_result;
#else
    throw std::runtime_error("--service is supported on Windows only");
#endif
}
}
