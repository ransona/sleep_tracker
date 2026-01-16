#include <Arduino.h>
#include <RotaryEncoder.h>
#include <Servo.h>
#include <string.h>

// Pin definitions
#define ENCODER_PIN1 2
#define ENCODER_PIN2 3
#define SERVO_PIN 9

// Servo angles
#define INITIAL_ANGLE 125
#define TARGET_ANGLE 175

// Thresholds
#define INACTIVITY_THRESHOLD 20000UL
#define POSITION_THRESHOLD 50

// Servo speeds
#define FORWARD_SPEED 200
#define BACKWARD_SPEED 50

RotaryEncoder encoder(ENCODER_PIN1, ENCODER_PIN2);
Servo servo;

unsigned long lastMovementTime = 0;
int lastEncoderPosition = 0;
bool movingToTarget = false;
bool automatic = true;

char lockstate[20] = "automatic";
char prevLockstate[20] = "automatic"; 

int locomotionBinary = 0;

// --- FUNCTION PROTOTYPE ---
void moveToAngle(int startAngle, int endAngle);

void setup() {
  Serial.begin(9600);

  servo.attach(SERVO_PIN);
  servo.write(INITIAL_ANGLE);

  lastMovementTime = millis();
  strcpy(prevLockstate, lockstate);  
}

void loop() {

  // --- SERIAL INPUT ---
  if (Serial.available() > 0) {
    char input = Serial.read();

    if (input == 'a') {
      automatic = true;
      strcpy(lockstate, "automatic");
    }
    else if (input == 'b') {
      automatic = false;
      strcpy(lockstate, "locked");
    }
    else if (input == 'c') {
      automatic = false;
      strcpy(lockstate, "unlocked");
    }
  }

  // --- ENCODER ---
  encoder.tick();
  int currentPosition = encoder.getPosition();

  // --- MOVEMENT DETECTION ---
  if (abs(currentPosition - lastEncoderPosition) > POSITION_THRESHOLD) {

    lastMovementTime = millis();
    locomotionBinary = 1;
    lastEncoderPosition = currentPosition;

    if (automatic && movingToTarget) {
      moveToAngle(servo.read(), INITIAL_ANGLE);
    }

    movingToTarget = false;
  }

  // --- INACTIVITY ---
  if (millis() - lastMovementTime >= INACTIVITY_THRESHOLD) {
    locomotionBinary = 0;

    if (automatic && !movingToTarget) {
      moveToAngle(servo.read(), TARGET_ANGLE);
      movingToTarget = true;
    }
  }

  // --- LOCK / UNLOCK (ONLY ON STATE CHANGE) ---
  if (strcasecmp(lockstate, prevLockstate) != 0) {

    if (strcasecmp(lockstate, "locked") == 0) {
      moveToAngle(servo.read(), TARGET_ANGLE);
    }
    else if (strcasecmp(lockstate, "unlocked") == 0) {
      moveToAngle(servo.read(), INITIAL_ANGLE);
    }

    strcpy(prevLockstate, lockstate);   
  }

  // --- DEBUG ---
  Serial.print(locomotionBinary);
  Serial.print(";");
  Serial.print(currentPosition);
  Serial.print(";");
  Serial.println(lockstate);
}

// --- SERVO MOVE ---
void moveToAngle(int startAngle, int endAngle) {

  if (startAngle < endAngle) {
    for (int angle = startAngle; angle <= endAngle; angle++) {
      servo.write(angle);
      delay(BACKWARD_SPEED);
    }
  } else {
    for (int angle = startAngle; angle >= endAngle; angle--) {
      servo.write(angle);
      delay(FORWARD_SPEED);
    }
  }
}
