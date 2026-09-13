enable_testing()

include(FetchContent)
FetchContent_Declare(
    unity
    GIT_REPOSITORY https://github.com/ThrowTheSwitch/Unity.git
    GIT_TAG v2.5.2
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(unity)

add_subdirectory(src)
add_subdirectory(tests)