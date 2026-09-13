#include <stdio.h>
#include <string.h>

static const char *VERSION = "0.0.1";

static void print_help(void) {
    printf("miniDB v%s - 教学性关系型数据库\n", VERSION);
    printf("\n");
    printf("阶段1：从零造数据库\n");
    printf("当前进度：章0 - 项目基础设施\n");
    printf("\n");
    printf("用法:\n");
    printf("  minidb              启动交互式 CLI\n");
    printf("  minidb --version    显示版本\n");
    printf("  minidb --help       显示帮助\n");
}

int main(int argc, char *argv[]) {
    if (argc > 1) {
        if (strcmp(argv[1], "--version") == 0) {
            printf("miniDB v%s\n", VERSION);
            return 0;
        }
        if (strcmp(argv[1], "--help") == 0) {
            print_help();
            return 0;
        }
    }

    print_help();
    printf("\n（CLI 将在章10 实现）\n");
    return 0;
}