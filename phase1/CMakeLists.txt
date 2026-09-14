enable_testing()

include(FetchContent)
FetchContent_Declare(
    unity
    GIT_REPOSITORY https://github.com/ThrowTheSwitch/Unity.git
    GIT_TAG v2.5.2
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(unity)

set(MINIDB_INCLUDE_DIRS
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/core
    ${CMAKE_CURRENT_SOURCE_DIR}/src/storage
    ${CMAKE_CURRENT_SOURCE_DIR}/src/txn
    ${CMAKE_CURRENT_SOURCE_DIR}/src/sql
    ${CMAKE_CURRENT_SOURCE_DIR}/src/optimizer
    ${CMAKE_CURRENT_SOURCE_DIR}/src/executor
    ${CMAKE_CURRENT_SOURCE_DIR}/src/executor/operators
    ${CMAKE_CURRENT_SOURCE_DIR}/src/server
)

add_library(minidb_core
    src/core/page.c
    src/core/file_manager.c
    src/core/pager.c
    src/core/replacer.c
    src/core/buffer_pool.c
    src/storage/btree_node.c
    src/storage/btree.c
    src/storage/schema.c
    src/storage/tuple.c
    src/storage/heap.c
    src/storage/table.c
    src/txn/wal.c
    src/txn/recovery.c
    src/txn/transaction.c
    src/txn/lock_manager.c
    src/txn/mvcc.c
    src/sql/ast.c
    src/sql/lexer.c
    src/sql/parser.c
    src/optimizer/catalog.c
    src/optimizer/logical_plan.c
    src/optimizer/optimizer.c
    src/executor/executor.c
    src/server/cli.c
)
target_include_directories(minidb_core PUBLIC ${MINIDB_INCLUDE_DIRS})

add_subdirectory(src)
add_subdirectory(tests)
