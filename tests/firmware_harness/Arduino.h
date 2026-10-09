// Just enough of the Arduino core to compile resistor_bank_controller.ino on a
// PC, for tests/test_firmware_bank_map.py. GPIO writes land in pinState[],
// Serial reads from a buffer the driver fills, and time only moves when the
// driver or the sketch's own delay() moves it.
#pragma once

#include <cstdio>
#include <string>

#define HIGH 1
#define LOW 0
#define OUTPUT 1

extern int pinState[20];
extern unsigned long fakeMillis;

inline void pinMode(int, int) {}
inline void digitalWrite(int pin, int value) { pinState[pin] = value; }
inline unsigned long millis() { return fakeMillis; }
inline void delay(unsigned long ms) { fakeMillis += ms; }
inline int min(int a, int b) { return a < b ? a : b; }

class String {
 public:
  String(const char *s = "") : s_(s) {}
  String(const std::string &s) : s_(s) {}
  unsigned int length() const { return s_.size(); }
  char charAt(unsigned int i) const { return i < s_.size() ? s_[i] : 0; }
  void setCharAt(unsigned int i, char c) { if (i < s_.size()) s_[i] = c; }
  void trim() {
    const char *ws = " \t\r\n";
    size_t a = s_.find_first_not_of(ws);
    if (a == std::string::npos) { s_.clear(); return; }
    s_ = s_.substr(a, s_.find_last_not_of(ws) - a + 1);
  }
  bool operator==(const String &o) const { return s_ == o.s_; }
  bool operator==(const char *o) const { return s_ == o; }
  const char *c_str() const { return s_.c_str(); }

 private:
  std::string s_;
};

class FakeSerial {
 public:
  std::string input;
  void begin(long) {}
  int available() { return (int)input.size(); }
  String readStringUntil(char end) {
    size_t n = input.find(end);
    std::string line = input.substr(0, n);
    input.erase(0, n == std::string::npos ? input.size() : n + 1);
    return String(line);
  }
  void print(const char *s) { std::printf("%s", s); }
  void print(const String &s) { std::printf("%s", s.c_str()); }
  void print(int v) { std::printf("%d", v); }
  void println(const char *s) { std::printf("%s\n", s); }
  void println(const String &s) { std::printf("%s\n", s.c_str()); }
  void println(int v) { std::printf("%d\n", v); }
};

extern FakeSerial Serial;
