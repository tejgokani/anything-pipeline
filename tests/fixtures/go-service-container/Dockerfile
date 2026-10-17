FROM golang:1.22 AS build
WORKDIR /src
COPY . .
RUN go build -o /svc .
