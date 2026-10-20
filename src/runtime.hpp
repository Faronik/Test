#pragma once
#include <filesystem>
#include <functional>
#include <string>
#include <vector>

namespace runtime {
struct Options {
    std::vector<std::string> args;
    bool service = false;
};
// Loads a JSON config without ever logging its contents. Relative paths are
// relative to the config file; resource defaults are relative to the executable.
Options configure(int argc, char** argv);
int run(bool service, const std::function<int()>& app);
void on_stop(std::function<void()> callback);
void ready();
bool stopping();
}
