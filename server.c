#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/in.h>

int main(int argc, char *argv[]) {
    if (argc != 4) {
        printf("Usage: %s <port> <content> <delay_ms>\n", argv[0]);
        return 1;
    }

    int port = atoi(argv[1]);
    char *content = argv[2];
    int delay_ms = atoi(argv[3]);

    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) {
        perror("Socket");
        return 1;
    }

    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port = htons(port);

    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        perror("Bind");
        close(server_fd);
        return 1;
    }

    if (listen(server_fd, 10) < 0) {
        perror("Listen");
        close(server_fd);
        return 1;
    }

    printf("Content server running on port %d\n", port);
    printf("Artificial response delay: %d ms\n", delay_ms);

    while (1) {
        int client_fd = accept(server_fd, NULL, NULL);
        if (client_fd < 0) {
            perror("Accept");
            continue;
        }

        char request[1024] = {0};
        ssize_t n = recv(client_fd, request, sizeof(request) - 1, 0);
        if (n > 0) {
            usleep((useconds_t)delay_ms * 1000);
            if (strncmp(request, "PING", 4) == 0) {
                send(client_fd, "PONG\n", 5, 0);
            } else if (strncmp(request, "GET", 3) == 0) {
                send(client_fd, content, strlen(content), 0);
                send(client_fd, "\n", 1, 0);
                printf("Content request served on port %d\n", port);
            } else {
                send(client_fd, "Unknown request\n", 16, 0);
            }
        }
        close(client_fd);
    }

    close(server_fd);
    return 0;
}
