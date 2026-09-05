#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 3) {
            fprintf(stderr, "缺少通知标题或内容\n");
            return 2;
        }

        NSString *title = [NSString stringWithUTF8String:argv[1]];
        NSString *body = [NSString stringWithUTF8String:argv[2]];
        UNUserNotificationCenter *center = [UNUserNotificationCenter currentNotificationCenter];
        dispatch_semaphore_t permissionSignal = dispatch_semaphore_create(0);
        __block BOOL granted = NO;
        __block NSError *permissionError = nil;

        [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert | UNAuthorizationOptionSound)
                              completionHandler:^(BOOL allowed, NSError *error) {
            granted = allowed;
            permissionError = error;
            dispatch_semaphore_signal(permissionSignal);
        }];
        dispatch_semaphore_wait(permissionSignal, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC));

        if (permissionError != nil) {
            fprintf(stderr, "通知授权失败：%s\n", permissionError.localizedDescription.UTF8String);
            return 2;
        }
        if (!granted) {
            fprintf(stderr, "系统通知权限未开启\n");
            return 3;
        }

        UNMutableNotificationContent *content = [[UNMutableNotificationContent alloc] init];
        content.title = title;
        content.body = body;
        content.sound = [UNNotificationSound defaultSound];
        UNTimeIntervalNotificationTrigger *trigger =
            [UNTimeIntervalNotificationTrigger triggerWithTimeInterval:0.1 repeats:NO];
        UNNotificationRequest *request =
            [UNNotificationRequest requestWithIdentifier:NSUUID.UUID.UUIDString content:content trigger:trigger];
        dispatch_semaphore_t sendSignal = dispatch_semaphore_create(0);
        __block NSError *sendError = nil;
        [center addNotificationRequest:request withCompletionHandler:^(NSError *error) {
            sendError = error;
            dispatch_semaphore_signal(sendSignal);
        }];
        dispatch_semaphore_wait(sendSignal, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC));
        if (sendError != nil) {
            fprintf(stderr, "发送通知失败：%s\n", sendError.localizedDescription.UTF8String);
            return 4;
        }
        [NSThread sleepForTimeInterval:0.5];
        return 0;
    }
}
