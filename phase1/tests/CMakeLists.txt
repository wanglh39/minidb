add_executable(test_sanity test_sanity.c)
target_link_libraries(test_sanity PRIVATE unity)
add_test(NAME sanity COMMAND test_sanity)